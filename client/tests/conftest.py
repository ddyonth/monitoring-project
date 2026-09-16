import os
import sys

# Делаем client/ импортируемым: `import client_agent`
CLIENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if CLIENT_DIR not in sys.path:
    sys.path.insert(0, CLIENT_DIR)
