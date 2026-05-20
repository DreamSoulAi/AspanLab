import io
import zipfile
from pathlib import Path
from fastapi import APIRouter
from fastapi.responses import StreamingResponse

router = APIRouter()

BASE = Path(__file__).parent.parent.parent   # trustcontrol/


@router.get("/installer")
async def download_installer():
    """Возвращает ZIP с файлами для установки на кассовый ПК."""
    buf = io.BytesIO()

    files = {
        "monitor.py":               BASE / "backend/worker/monitor.py",
        "requirements-monitor.txt": BASE / "requirements-monitor.txt",
        "1_SETUP.bat":              BASE / "scripts/windows/1_SETUP.bat",
        "2_CONFIG.bat":             BASE / "scripts/windows/2_CONFIG.bat",
        "3_RUN.bat":                BASE / "scripts/windows/3_RUN.bat",
        "АВТОЗАПУСК.bat":           BASE / "scripts/windows/АВТОЗАПУСК.bat",
        "config.ini":               BASE / "scripts/windows/config.ini",
        "README.txt":               BASE / "scripts/windows/README.txt",
    }

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, path in files.items():
            if path.exists():
                zf.write(path, f"TrustControl/{name}")

    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=TrustControl_installer.zip"},
    )
