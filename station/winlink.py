"""Winlink Express (formerly RMS Express) outbox writer for ICS forms.

Format learned from Winlink Express 1.8.0.0 on the test PC (2026-11-02, the owner's test ICS-213 and Form 309
posted to the Outbox, then read back):
  <install>\\<account>\\Messages\\<MID>.mime   RFC-822 message, CRLF; multipart/mixed:
        text/plain iso-8859-1 quoted-printable (the template's Msg: section, filled)
        text/xml base64 RMS_Express_Form_<viewer>.xml   (form_parameters + variables -> Express redraws the form)
  <install>\\<account>\\MessagesBackup\\<MID>.mime   identical copy
  <install>\\<account>\\Data\\Registry.txt     one line per message, fields separated by 0x01, folder "Outbox"
  <install>\\<account>\\Data\\Mids Seen.txt    "<MID>\\x01<yyyy/mm/dd HH:MM>"
  <install>\\<account>\\Data\\Destinations.txt (UTF-8 BOM) "<MID>\\x0145<size>\\x01<to>\\x01"
Several recipients (owner's 2-recipient ICS-213, 11-02): the To: header folds one address per line
(",\r\n "), Registry's recipient field is the FIRST call + "..." , Destinations.txt has one line per recipient.
The size field: LZHUF-compressed B2F size; ours (station/lzhuf.py) lands within ~5 % of Express's (1584 vs 1663),
so it is an estimate; Express appears to use it only for display. Winlink Express must be CLOSED while we write (it keeps its own index in
memory); every write backs up the Data files first. The first real write must be checked by the operator.
"""
import base64
import email.utils
import os
import quopri
import random
import re
import shutil
import string
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.sax.saxutils import escape

SEP = "\x01"
DEFAULT_DIR = r"C:\RMS Express"
EXPRESS_VERSION = "1.8.0.0"
NOT_ACCOUNTS = {"Global Folders", "Standard Templates", "Dictionary", "MapCache", "iniBackup", "Temp"}
TEMPLATES = {"ics309": ("ICS USA Forms", "Form-309 Communications Log.txt"),
             "ics213": ("ICS USA Forms", "ICS213 General Message.txt")}


# ---------- discovery ----------
def find_installs(extra=()) -> list[str]:
    """Every folder holding 'RMS Express.exe': the default, the Windows uninstall registry, and `extra`."""
    cands = [DEFAULT_DIR, *extra]
    if os.name == "nt":
        try:
            import winreg
            for root in (r"Software\Microsoft\Windows\CurrentVersion\Uninstall",
                         r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"):
                try:
                    k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, root)
                except OSError:
                    continue
                for i in range(winreg.QueryInfoKey(k)[0]):
                    try:
                        sk = winreg.OpenKey(k, winreg.EnumKey(k, i))
                        name = winreg.QueryValueEx(sk, "DisplayName")[0]
                        if re.search(r"Winlink|RMS Express", name, re.I):
                            cands.append(winreg.QueryValueEx(sk, "InstallLocation")[0])
                    except OSError:
                        continue
        except ImportError:
            pass
    out = []
    for c in cands:
        p = str(Path(c)).rstrip("\\/")
        if c and (Path(p) / "RMS Express.exe").exists() and p.lower() not in [o.lower() for o in out]:
            out.append(p)
    return out


def accounts(install: str) -> list[str]:
    """Callsign folders (each has its own Messages + Data = its own Outbox)."""
    base = Path(install)
    return sorted(d.name for d in base.iterdir()
                  if d.is_dir() and d.name not in NOT_ACCOUNTS and (d / "Messages").is_dir() and (d / "Data").is_dir())


def active_callsign(install: str) -> str | None:
    ini = Path(install) / "RMS Express.ini"
    try:
        m = re.search(r"^\[Properties\][^\[]*?^Callsign=(\S+)", ini.read_text(errors="replace"), re.M | re.S)
        return m.group(1) if m else None
    except OSError:
        return None


def express_running() -> bool:
    if os.name != "nt":
        return False
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq RMS Express.exe", "/NH"], capture_output=True,
                             text=True, timeout=10).stdout
        return "RMS Express.exe" in out
    except (OSError, subprocess.SubprocessError):
        return False


def describe(install: str) -> dict:
    return {"path": install, "accounts": accounts(install), "active": active_callsign(install),
            "templates": all((Path(install) / "Standard Templates" / d / f).exists() for d, f in TEMPLATES.values())}


# ---------- templates ----------
def load_template(install: str, form: str) -> dict:
    d, f = TEMPLATES[form]
    text = (Path(install) / "Standard Templates" / d / f).read_text(encoding="latin-1")
    head, _, msg = text.partition("\nMsg:")
    t = {"msg": msg.lstrip("\r\n"), "file": f}
    for line in head.splitlines():
        k, _, v = line.partition(":")
        t[k.strip().lower()] = v.strip()
    forms = [x.strip() for x in t.get("form", "").split(",")]
    t["initial"], t["viewer"] = forms[0], (forms[1] if len(forms) > 1 else forms[0])
    t["vars"] = sorted(set(re.findall(r"<var (\w+)>", text)))
    # the viewer's box sizes: a value longer than its box overflows Express's form view (smaller-looking text)
    t["limits"] = {}
    try:
        page = (Path(install) / "Standard Templates" / d / t["viewer"]).read_text(encoding="latin-1")
        for tag in re.findall(r"<input\b[^>]*>", page, re.I):
            name = re.search(r'\{var (\w+)\}', tag) or re.search(r'name="(\w+)"', tag)
            size = re.search(r'maxlength="(\d+)"', tag)
            if name and size:
                t["limits"][name.group(1).lower()] = int(size.group(1))
    except OSError:
        pass
    return t


def fill(s: str, values: dict, sender: str) -> str:
    s = re.sub(r"<var (\w+)>", lambda m: str(values.get(m.group(1), values.get(m.group(1).lower(), ""))), s)
    return s.replace("<MsgSender>", sender).replace("<ProgramVersion>", EXPRESS_VERSION)


# ---------- message ----------
def new_mid() -> str:
    return "".join(random.choice(string.ascii_uppercase + string.digits) for _ in range(12))


def grid_location(grid: str | None) -> str | None:
    """Maidenhead (4 or 6 chars) -> centre as Express writes it: '39.812500N, 89.625000W (Grid square)'."""
    if not grid or not re.fullmatch(r"[A-Ra-r]{2}\d{2}([A-Xa-x]{2})?", grid):
        return None
    g = grid.upper()
    lon = (ord(g[0]) - 65) * 20 - 180 + int(g[2]) * 2
    lat = (ord(g[1]) - 65) * 10 - 90 + int(g[3])
    if len(g) == 6:
        lon += (ord(g[4]) - 65) * (2 / 24) + 1 / 24
        lat += (ord(g[5]) - 65) * (1 / 24) + 1 / 48
    else:
        lon, lat = lon + 1, lat + 0.5
    return f"{abs(lat):.6f}{'N' if lat >= 0 else 'S'}, {abs(lon):.6f}{'W' if lon < 0 else 'E'} (Grid square)"


def build(form: str, template: dict, values: dict, sender: str, to: list[str], grid: str | None = None,
          now: datetime | None = None, mid: str | None = None) -> dict:
    """One message: {'mid','subject','to','mime' (bytes),'size','date'}."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    from .ics import fit
    lim = template.get("limits") or {}
    values = {k: fit(v, lim[k.lower()]) if k.lower() in lim and len(str(v)) > lim[k.lower()] else v for k, v in values.items()}
    mid = mid or new_mid()
    subject = fill(template.get("subject", ""), values, sender)
    body = fill(template["msg"], values, sender).replace("\r\n", "\n").replace("\n", "\r\n")
    viewer = template["viewer"]
    xml_vars = {"msgto": "", "msgcc": "", "msgsender": sender, "msgsubject": "", "msgbody": "", "msgp2p": "False",
                "msgro": "False", "msgpostoffice": "False", "msgisreply": "False", "msgisforward": "False",
                "msgisacknowledgement": "False", "msgseqnum": "0"} | {k.lower(): v for k, v in values.items()}
    xml = ("<?xml version=\"1.0\"?>\r\n<RMS_Express_Form>\r\n  <form_parameters>\r\n"
           "    <xml_file_version>1.0</xml_file_version>\r\n"
           f"    <rms_express_version>{EXPRESS_VERSION}</rms_express_version>\r\n"
           f"    <submission_datetime>{now:%Y%m%d%H%M%S}</submission_datetime>\r\n"
           f"    <senders_callsign>{escape(sender)}</senders_callsign>\r\n"
           f"    <grid_square>{escape((grid or '').upper())}</grid_square>\r\n"
           f"    <display_form>{escape(viewer)}</display_form>\r\n"
           f"    <reply_template>{escape(template.get('replytemplate', ''))}</reply_template>\r\n"
           "  </form_parameters>\r\n  <variables>\r\n"
           + "".join(f"    <{k}>{escape(str(v))}</{k}>\r\n" for k, v in xml_vars.items())
           + "  </variables>\r\n</RMS_Express_Form>\r\n")
    xml_name = f"RMS_Express_Form_{viewer.rsplit('.', 1)[0]}.xml"
    boundary = "boundary" + base64.b64encode(os.urandom(6)).decode()
    b64 = lambda b: "\r\n".join(base64.b64encode(b).decode()[i:i + 76] for i in range(0, len(base64.b64encode(b)), 76))
    qp = quopri.encodestring(body.encode("iso-8859-1", "replace")).decode("ascii").replace("\r\n", "\n").replace("\n", "\r\n")
    loc = grid_location(grid)
    headers = [f"Date: {email.utils.format_datetime(now)}", f"From: {sender}@winlink.org",
               f"Reply-To: {sender}@winlink.org", f"Subject: {subject}",
               "To: " + ",\r\n ".join(f"{c}@winlink.org" for c in to), f"Message-ID: {mid}",
               f"X-Cancel: {now + timedelta(days=21):%Y/%m/%d %H:%M}", f"X-Source: {sender}"]
    if loc:
        headers.append(f"X-Location: {loc}")
    headers += ["MIME-Version: 1.0", "MIME-Version: 1.0", f'Content-Type: multipart/mixed; boundary="{boundary}"']
    mime = ("\r\n".join(headers) + "\r\n\r\n"
            f"--{boundary}\r\nContent-Type: text/plain; charset=\"iso-8859-1\"\r\n"
            f"Content-Transfer-Encoding: quoted-printable\r\n\r\n{qp}\r\n"
            f"--{boundary}\r\nContent-Disposition: attachment;\r\n name=\"{xml_name}\";\r\n filename=\"{xml_name}\"\r\n"
            f"Content-Type: text/xml;\r\n name=\"{xml_name}\"\r\nContent-Transfer-Encoding: base64\r\n\r\n"
            f"{b64(xml.encode('utf-8'))}\r\n--{boundary}--\r\n").encode("latin-1", "replace")
    size = b2f_size(mid, now, sender, to, subject, body.encode("iso-8859-1", "replace"), [(xml_name, xml.encode("utf-8"))])
    return {"mid": mid, "subject": subject, "to": list(to), "mime": mime, "size": size, "date": now,
            "form": form, "location": loc or "", "body": body}


def b2f_size(mid, now, sender, to, subject, body: bytes, files) -> int:
    """Estimate of Express's size field: the LZHUF size of the message in Winlink's B2F transfer form."""
    from .lzhuf import compressed_size
    h = [f"Mid: {mid}", f"Date: {now:%Y/%m/%d %H:%M}", "Type: Private", f"From: {sender}", *[f"To: {c}" for c in to],
         f"Subject: {subject}", f"Mbo: {sender}", f"Body: {len(body)}", *[f"File: {len(b)} {n}" for n, b in files]]
    data = "\r\n".join(h).encode("latin-1", "replace") + b"\r\n\r\n" + body
    for _, b in files:
        data += b"\r\n" + b
    return compressed_size(data + b"\r\n")


# ---------- outbox ----------
def registry_line(msg: dict, sender: str) -> str:
    d = msg["date"]
    f = [msg["mid"], f"{d:%Y/%m/%d %H:%M}", sender, sender, "2", "True|True||||True|", str(msg["size"]),
         msg["subject"], "Outbox", "", "", "", "C", "False", "", "",
         msg["to"][0] + ("..." if len(msg["to"]) > 1 else ""), "0", "",
         f"{d:%Y%m%d%H%M%S}", "0", msg["location"]]
    return SEP.join(f)


def put_in_outbox(install: str, account: str, messages: list[dict], check_running=None) -> dict:
    if (check_running or express_running)():
        raise RuntimeError("Winlink Express is open. Close it, then try again (it would overwrite the Outbox list).")
    acc = Path(install) / account
    if not (acc / "Messages").is_dir() or not (acc / "Data").is_dir():
        raise FileNotFoundError(f"{acc} is not a Winlink Express callsign folder")
    data, stamp = acc / "Data", datetime.now().strftime("%Y%m%d%H%M%S")
    (data / "Backup").mkdir(exist_ok=True)
    for name in ("Registry.txt", "Mids Seen.txt", "Destinations.txt"):
        if (data / name).exists():
            shutil.copy2(data / name, data / "Backup" / f"{Path(name).stem}_hamnet_{stamp}{Path(name).suffix}")
    (acc / "MessagesBackup").mkdir(exist_ok=True)

    def append(path: Path, line: str, bom=False):
        new = not path.exists() or path.stat().st_size == 0
        with open(path, "ab") as fh:
            if new and bom:
                fh.write(b"\xef\xbb\xbf")
            fh.write(line.encode("utf-8") + b"\r\n")
    written = []
    for m in messages:
        for folder in ("Messages", "MessagesBackup"):
            (acc / folder / f"{m['mid']}.mime").write_bytes(m["mime"])
        append(data / "Registry.txt", registry_line(m, account))
        append(data / "Mids Seen.txt", f"{m['mid']}{SEP}{m['date']:%Y/%m/%d %H:%M}")
        for c in m["to"]:                                  # one line per recipient, as Express writes them
            append(data / "Destinations.txt", f"{m['mid']}{SEP}45{m['size']}{SEP}{c}{SEP}", bom=True)
        written.append({"mid": m["mid"], "subject": m["subject"], "to": m["to"]})
    return {"written": written, "account": account, "install": install, "backup": stamp}
