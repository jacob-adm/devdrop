# SPDX-License-Identifier: Apache-2.0
"""JUnit XML for a devdrop run: one testcase per device, so CI dashboards show each device."""

import xml.etree.ElementTree as ET


def write(path, command, records):
    """Write one <testsuite> for this run; skipped and failed devices carry devdrop's reason."""
    failures = sum(r["result"] == "failed" for r in records)
    skipped = sum(r["result"] == "skipped" for r in records)
    total = sum(r.get("seconds", 0) for r in records)
    suite = ET.Element("testsuite", name=f"devdrop {command}", tests=str(len(records)),
                       failures=str(failures), errors="0", skipped=str(skipped), time=f"{total:.1f}")
    for r in records:
        case = ET.SubElement(suite, "testcase", name=r.get("name") or r["id"],
                             classname=f"{r['platform']}.{r.get('kind') or 'device'}",
                             time=f"{r.get('seconds', 0):.1f}")
        message = f"{r['stage']}: {r['reason']}" if r.get("reason") else r["stage"]
        if r["result"] == "failed":
            ET.SubElement(case, "failure", message=message, type=r["stage"]).text = message
        elif r["result"] == "skipped":
            ET.SubElement(case, "skipped", message=message)
        details = [f"device: {r['id']}"]
        details += [f"{k}: {r[k]}" for k in ("manufacturer", "model", "os_version", "api") if r.get(k)]
        details += [f"warning: {w}" for w in r.get("warnings", [])]
        if r.get("log"):
            details.append(f"log: {r['log']}")
        ET.SubElement(case, "system-out").text = "\n".join(details)
    tree = ET.ElementTree(ET.Element("testsuites"))
    tree.getroot().append(suite)
    ET.indent(tree)
    tree.write(path, encoding="utf-8", xml_declaration=True)
