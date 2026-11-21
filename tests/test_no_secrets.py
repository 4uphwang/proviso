"""저장소에 들어가는 파일에 법령 API 인증값(OC)이 남지 않았는지 확인한다."""
import gzip
import re
import subprocess

from proviso.config import ROOT

OC_IN_URL = re.compile(r"OC=(?!REDACTED|<OC>|test\b|\$|\{)[A-Za-z0-9]+")


def test_tracked_files_have_no_oc_value():
    files = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.split()
    leaked = []
    for f in files:
        try:
            path = ROOT / f
            text = (gzip.decompress(path.read_bytes()).decode("utf-8") if f.endswith(".gz")
                    else path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, FileNotFoundError):
            continue
        if OC_IN_URL.search(text):
            leaked.append(f)
    assert not leaked, f"인증값이 들어 있는 파일: {leaked}"
