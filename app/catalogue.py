import csv
import io
import json
from pathlib import Path

def load_catalogues(path="fixtures/catalogues.json"):
    data=json.loads(Path(path).read_text())
    for venue,languages in data.items():
        for language,rows in languages.items():
            if any(not r.get("service") for r in rows): raise ValueError("服务项目缺少名称")
    return data

def export_csv(venue,language,rows):
    stream=io.StringIO(newline="")
    writer=csv.DictWriter(stream,fieldnames=["venue","language","service"])
    writer.writeheader()
    for row in rows: writer.writerow({"venue":venue,"language":language,"service":row["service"]})
    return stream.getvalue().encode("utf-8-sig")
