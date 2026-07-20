"""
sspcloud_mcp.repo_sync — Envoi d'un repo (GitHub ou local) dans le pod.

Deux modes, détectés depuis la source :
  URL git   → git clone dans le pod (egress SSPCloud ouvert). Versionné.
  chemin    → tar.gz local → transfert base64 via le kernel → extraction.
              Marche sur TOUT transport (aucun kubectl requis).

Le transfert local passe par le kernel (exec Python), donc borné en taille :
au-delà de _MAX_LOCAL_MB on refuse avec un hint (utiliser un remote git).
"""

from __future__ import annotations

import base64
import io
import re
import tarfile
from pathlib import Path

from .errors import bad_source, MCPToolError

_MAX_LOCAL_MB = 40
_GIT_RE = re.compile(r"^(https?://|git@|ssh://).*|^[\w.-]+/[\w.-]+$")


def _is_git_url(source: str) -> bool:
    if source.startswith(("http://", "https://", "git@", "ssh://")):
        return True
    # forme courte "org/repo" (GitHub) — mais pas un chemin local existant
    if re.match(r"^[\w.-]+/[\w.-]+$", source) and not Path(source).exists():
        return True
    return False


def _normalize_git(source: str) -> str:
    if source.startswith(("http://", "https://", "git@", "ssh://")):
        return source
    if re.match(r"^[\w.-]+/[\w.-]+$", source):
        return f"https://github.com/{source}.git"
    return source


async def push_repo(mgr, session_id: str, source: str, *,
                    dest: str = "", branch: str = "") -> dict:
    """Envoie `source` dans le pod de la session. Retourne {path, method, sha}."""
    s = mgr.get(session_id)

    if _is_git_url(source):
        return await _push_git(mgr, session_id, s, source, dest, branch)
    p = Path(source)
    if not p.exists():
        raise bad_source(source)
    return await _push_local(mgr, session_id, s, p, dest)


async def _push_git(mgr, session_id, s, source, dest, branch) -> dict:
    url = _normalize_git(source)
    name = dest or re.sub(r"\.git$", "", url.rstrip("/").split("/")[-1])
    target = f"{s.workdir}/{name}"
    br = f"-b {branch}" if branch else ""
    cmd = (f"rm -rf {target} && git clone --depth 1 {br} {url} {target} 2>&1 "
           f"&& cd {target} && git rev-parse --short HEAD")
    r = await mgr.exec_bash(session_id, cmd, timeout=300)
    if r["rc"] != 0:
        raise MCPToolError("GIT_CLONE_FAILED",
                           f"git clone a échoué : {r['stdout'][-200:]}",
                           "Vérifiez l'URL, la branche, ou l'accès (repo privé "
                           "→ token). L'egress du pod est ouvert.")
    sha = r["stdout"].strip().splitlines()[-1] if r["stdout"].strip() else ""
    s.repo_path = target
    mgr._save()
    return {"path": target, "method": "git-clone", "sha": sha}


async def _push_local(mgr, session_id, s, path: Path, dest) -> dict:
    # tar.gz en mémoire (exclut .git, caches lourds)
    buf = io.BytesIO()
    excl = {".git", "__pycache__", ".venv", "node_modules", ".mypy_cache"}

    def _filter(ti: tarfile.TarInfo):
        parts = set(Path(ti.name).parts)
        return None if parts & excl else ti

    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(str(path), arcname=path.name, filter=_filter)
    data = buf.getvalue()
    size_mb = len(data) / 1e6
    if size_mb > _MAX_LOCAL_MB:
        raise MCPToolError(
            "REPO_TOO_LARGE",
            f"Repo local trop volumineux ({size_mb:.1f} Mo > {_MAX_LOCAL_MB} Mo).",
            "Poussez-le sur un remote git et utilisez l'URL, ou réduisez le contenu.")

    name = dest or path.name
    target = f"{s.workdir}/{name}"
    b64 = base64.b64encode(data).decode()
    # Écriture + extraction via le kernel (transport-agnostique).
    code = (
        "import base64, tarfile, io, os, shutil\n"
        f"_raw = base64.b64decode({b64!r})\n"
        f"_dst = {target!r}\n"
        "shutil.rmtree(_dst, ignore_errors=True)\n"
        f"os.makedirs({s.workdir!r}, exist_ok=True)\n"
        f"tarfile.open(fileobj=io.BytesIO(_raw), mode='r:gz').extractall({s.workdir!r})\n"
        "print('EXTRACT_OK', _dst)\n"
    )
    res = await mgr.exec_python(session_id, code, timeout=300)
    if "EXTRACT_OK" not in res.stdout:
        raise MCPToolError("UPLOAD_FAILED",
                           f"Extraction échouée : {(res.error or {}).get('evalue','')}",
                           "Réessayez ; si le repo est volumineux, préférez un remote git.")
    s.repo_path = target
    mgr._save()
    return {"path": target, "method": "local-tar", "sha": "", "size_mb": round(size_mb, 2)}


async def pull_artifact(mgr, session_id: str, remote_path: str,
                        local_path: str = "") -> dict:
    """Rapatrie un fichier du pod vers le PC (base64 via kernel)."""
    s = mgr.get(session_id)
    rp = remote_path if remote_path.startswith("/") else f"{s.workdir}/{remote_path}"
    code = (
        "import base64, os\n"
        f"_p = {rp!r}\n"
        "print('SIZE', os.path.getsize(_p))\n"
        "print('DATA', base64.b64encode(open(_p,'rb').read()).decode())\n"
    )
    res = await mgr.exec_python(session_id, code, timeout=300)
    if "DATA " not in res.stdout:
        raise MCPToolError("PULL_FAILED",
                           f"Lecture impossible : {(res.error or {}).get('evalue', remote_path)}",
                           "Vérifiez le chemin dans le pod (list_files).")
    b64 = res.stdout.split("DATA ", 1)[1].strip()
    data = base64.b64decode(b64)
    out = Path(local_path or Path.cwd() / Path(remote_path).name)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    return {"local_path": str(out), "bytes": len(data)}
