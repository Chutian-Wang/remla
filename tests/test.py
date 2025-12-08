import os
from pathlib import Path
boot_time = Path("boottime")
print(boot_time.read_text().strip())
import psutil

print(psutil.boot_time())
boot_time.write_text(str(int(psutil.boot_time())))
