"""
Сквозной тест самообновления на настоящих PyInstaller-сборках агента.

Запускается только если заданы переменные окружения (в CI — джоба
selfupdate-e2e-windows в .github/workflows/ci.yml), иначе пропускается:
  MONITORING_SELFUPDATE_E2E_OLD_EXE     — exe текущей версии (client/VERSION);
  MONITORING_SELFUPDATE_E2E_NEW_EXE     — exe с более новой версией;
  MONITORING_SELFUPDATE_E2E_NEW_VERSION — версия, с которой собран NEW_EXE.

Стаб сервера релизов слушает 127.0.0.1:8000 — встроенный адрес сервера
(DEFAULT_CONFIG). Тест намеренно не задаёт MONITORING_SERVER_URL, чтобы exe
проверялся в поставочной конфигурации по умолчанию (файл конфигурации агент
не читает, см. README).

Что проверяется на живом exe (в отличие от юнит-тестов):
  - замена работающего exe самим собой через os.replace (на Windows файл
    запущенного процесса нельзя удалить, но можно переименовать);
  - перезапуск новой версии отсоединённым процессом (DETACHED_PROCESS, без
    служебных переменных PyInstaller) — он сам ходит на сервер и завершается;
  - удаление .old при следующем старте;
  - отказ от релиза с неверным sha256 без замены exe.

Планировщик заданий (AtLogOn/watchdog) здесь не проверяется: на CI-раннере
нет интерактивной сессии входа, это остаётся ручной проверкой.
"""

import hashlib
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import psutil
import pytest
from release_stub import CLIENT_KEY, FakeReleaseServer

STUB_HOST, STUB_PORT = "127.0.0.1", 8000      # встроенный адрес сервера в exe
EXPECTED_PLATFORM = "windows" if os.name == "nt" else "linux"   # что exe шлёт в X-Client-Platform
AGENT_TIMEOUT = 180                            # с: один запуск exe с --apply-update-now
CHILD_TIMEOUT = 120                            # с: ожидание завершения перезапущенного exe

ENV_OLD, ENV_NEW, ENV_NEW_VERSION = (
    "MONITORING_SELFUPDATE_E2E_OLD_EXE",
    "MONITORING_SELFUPDATE_E2E_NEW_EXE",
    "MONITORING_SELFUPDATE_E2E_NEW_VERSION",
)

pytestmark = pytest.mark.skipif(
    not all(os.environ.get(v) for v in (ENV_OLD, ENV_NEW, ENV_NEW_VERSION)),
    reason=f"сквозной тест на настоящих exe: нужны {ENV_OLD}, {ENV_NEW}, {ENV_NEW_VERSION}",
)


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class Builds:
    old_exe: str
    new_exe: str
    new_version: str
    old_sha: str
    new_sha: str
    new_bytes: bytes


@pytest.fixture(scope="module")
def builds() -> Builds:
    old_exe, new_exe = os.environ[ENV_OLD], os.environ[ENV_NEW]
    new_version = os.environ[ENV_NEW_VERSION].strip()
    for p in (old_exe, new_exe):
        assert os.path.isfile(p), f"нет файла сборки: {p}"
    b = Builds(old_exe, new_exe, new_version, sha256_file(old_exe), sha256_file(new_exe),
               Path(new_exe).read_bytes())
    assert b.old_sha != b.new_sha, "старая и новая сборки одинаковы — версия не вшилась?"
    print(f"\nold exe {old_exe} sha256={b.old_sha}\nnew exe {new_exe} sha256={b.new_sha} version={new_version}")
    return b


@pytest.fixture(scope="module")
def stub():
    try:
        s = FakeReleaseServer(STUB_HOST, STUB_PORT).start()
    except OSError as e:
        pytest.fail(f"не удалось занять {STUB_HOST}:{STUB_PORT} для стаба сервера релизов "
                    f"(exe ходит только на этот адрес): {e}")
    yield s
    s.stop()


@pytest.fixture
def srv(stub):
    stub.release, stub.data = None, b""
    stub.releases.clear()
    stub.requests.clear()
    return stub


@pytest.fixture
def workdir(tmp_path, builds):
    """Каталог агента с копией СТАРОЙ exe под именем client_agent.exe."""
    d = tmp_path / "agent"
    d.mkdir()
    shutil.copy2(builds.old_exe, d / "client_agent.exe")   # copy2 переносит права (бит x на POSIX)
    return d


def run_agent(exe: Path, cwd: Path) -> subprocess.CompletedProcess:
    """Один запуск exe с --apply-update-now; ключ обновления — только через env, как в проде."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("MONITORING_")}
    env["MONITORING_CLIENT_UPDATE_KEY"] = CLIENT_KEY
    t0 = time.monotonic()
    r = subprocess.run([str(exe), "--apply-update-now"], cwd=str(cwd), env=env,
                       capture_output=True, text=True, errors="replace", timeout=AGENT_TIMEOUT, check=False)
    print(f"\n$ {exe.name} --apply-update-now -> exit {r.returncode} ({time.monotonic() - t0:.1f}s)")
    for stream, text in (("stdout", r.stdout), ("stderr", r.stderr)):
        if text.strip():
            print(f"--- {stream} ---\n{text.rstrip()}")
    return r


def agent_processes(workdir: Path):
    """Процессы, чей исполняемый файл лежит в каталоге агента (перезапущенная новая версия)."""
    want = os.path.normcase(os.path.realpath(str(workdir)))
    found = []
    for p in psutil.process_iter(["pid", "exe"]):
        exe = p.info.get("exe")
        if exe and os.path.normcase(os.path.dirname(os.path.realpath(exe))) == want:
            found.append(p)
    return found


def wait_agents_exit(workdir: Path, timeout: float = CHILD_TIMEOUT):
    """Ждёт, пока в каталоге агента не останется запущенных процессов; возвращает замеченные pid."""
    seen, deadline = set(), time.monotonic() + timeout
    while True:
        procs = agent_processes(workdir)
        seen.update(p.pid for p in procs)
        if not procs:
            return sorted(seen)
        if time.monotonic() > deadline:
            for p in procs:
                p.kill()
            pytest.fail(f"агент не завершился за {timeout}s: pids {[p.pid for p in procs]}")
        time.sleep(0.05)


class OldFileWatcher:
    """
    Фоновый наблюдатель за <exe>.old.

    Старый exe после замены сам запускает новую версию, а та при старте
    удаляет .old (cleanup_old_executable), поэтому проверять .old "после"
    нельзя — файл существует от момента os.replace в старом процессе до
    старта Python в перезапущенной новой версии (распаковка onefile-exe
    занимает секунды). Наблюдатель опрашивает файл каждые 10 мс и запоминает
    его sha256 — по нему видно, что в .old ушла именно старая версия.
    """

    def __init__(self, old_path: Path):
        self.path = old_path
        self.seen = False
        self.sha = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self._stop.is_set():
            if self.path.exists():
                self.seen = True
                if self.sha is None:
                    try:
                        self.sha = sha256_file(self.path)
                    except OSError:
                        pass          # удалён во время чтения — попробуем ещё раз
            self._stop.wait(0.01)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(5)


# ------------------------------------------------------------ сценарии

def test_update_replaces_running_exe_then_stays_up_to_date(builds, srv, workdir):
    exe, old_file, download_file = (workdir / "client_agent.exe", workdir / "client_agent.exe.old",
                                    workdir / "client_agent.exe.download")
    # релиз опубликован только под ОС прогона: exe получит его, лишь прислав
    # верный X-Client-Platform (на Linux без заголовка стаб отдал бы windows-релиз, т.е. 404)
    srv.set_release(builds.new_version, builds.new_bytes, platform=EXPECTED_PLATFORM)

    # 1) старая версия находит релиз, скачивает, заменяет саму себя и
    #    перезапускает новую версию с теми же аргументами
    with OldFileWatcher(old_file) as watcher:
        r = run_agent(exe, workdir)
        assert r.returncode == 0, "старая версия должна завершиться с кодом 0 после обновления"
        child_pids = wait_agents_exit(workdir)
    print(f"перезапущенные процессы: {child_pids}; .old замечен: {watcher.seen}, "
          f"sha256={watcher.sha}, остался после всего: {old_file.exists()}")

    assert sha256_file(exe) == builds.new_sha, "на месте старой exe должна лежать новая"
    assert watcher.seen and watcher.sha == builds.old_sha, "старая exe должна была уехать в .old"
    assert not download_file.exists(), ".download после установки не должен оставаться"
    # перезапущенная новая версия дошла до сервера (третий запрос) и сочла себя актуальной
    assert [r.path for r in srv.requests] == [
        "/api/client-release", "/api/download/client-agent", "/api/client-release",
    ], srv.requests
    assert all(r.client_key == CLIENT_KEY for r in srv.requests)
    assert all(r.platform == EXPECTED_PLATFORM for r in srv.requests), srv.requests
    assert child_pids, "перезапуск новой версии не состоялся (процесс в каталоге агента не замечен)"

    # 2) повторный запуск уже новой версии: up_to_date, без скачивания, exe не трогается
    srv.requests.clear()
    r2 = run_agent(exe, workdir)
    assert r2.returncode == 0
    assert wait_agents_exit(workdir) == [], "up_to_date не должен ничего перезапускать"
    assert sha256_file(exe) == builds.new_sha
    assert [r.path for r in srv.requests] == ["/api/client-release"], srv.requests  # download не запрашивался
    assert not download_file.exists()
    assert not old_file.exists(), ".old должен быть удалён при старте новой версии"


def test_release_with_wrong_sha256_is_rejected(builds, srv, workdir):
    exe, old_file, download_file = (workdir / "client_agent.exe", workdir / "client_agent.exe.old",
                                    workdir / "client_agent.exe.download")
    # сервер отдаёт байты новой версии, но объявляет чужой sha256 — подмена/битая загрузка
    srv.set_release(builds.new_version, builds.new_bytes,
                    sha256=hashlib.sha256(builds.new_bytes + b"tampered").hexdigest())

    r = run_agent(exe, workdir)
    assert r.returncode == 1, "несовпадение sha256 — это ошибка обновления (код 1)"
    assert wait_agents_exit(workdir) == [], "при отказе ничего не должно перезапускаться"

    assert sha256_file(exe) == builds.old_sha, "exe не должен был замениться"
    assert not old_file.exists(), ".old при отказе не создаётся"
    assert not download_file.exists(), "скачанный файл с неверным sha256 должен быть удалён"
    assert [r.path for r in srv.requests] == ["/api/client-release", "/api/download/client-agent"], srv.requests
