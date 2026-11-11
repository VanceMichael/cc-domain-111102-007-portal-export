from pathlib import Path
from app.wire import Replay
from app.catalogue import load_catalogues
print({k:list(v) for k,v in load_catalogues().items()})
