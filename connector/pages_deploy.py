"""Explicit Windows-only Pages Direct Upload. Never exposed as HTTP command."""
import argparse
import json
import re
import shutil
import subprocess
from pathlib import Path

def invoke(parts,root,timeout):
    p=subprocess.run(parts,cwd=str(root),shell=False,text=True,capture_output=True,timeout=timeout)
    if p.returncode:
        raise RuntimeError(parts[0]+" failed: "+(p.stderr or p.stdout)[-1600:])
    return p.stdout.strip()

def main():
    parser=argparse.ArgumentParser(description="WebManager local Cloudflare Pages deploy (manual, verified)")
    parser.add_argument("--root",required=True,help="Existing checked-out GitHub repository folder")
    parser.add_argument("--project",required=True,help="Existing Cloudflare Pages project")
    parser.add_argument("--branch",default="main")
    parser.add_argument("--output",default="dist")
    parser.add_argument("--script",default="build")
    parser.add_argument("--deploy",action="store_true",help="Actually upload after preflight; omitted = read-only")
    args=parser.parse_args()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}",args.project):raise SystemExit("Invalid Pages project")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,95}",args.branch) or ".." in args.branch:raise SystemExit("Invalid branch")
    if not re.fullmatch(r"[a-zA-Z0-9:_-]{1,60}",args.script):raise SystemExit("Invalid npm script name")
    rel=Path(args.output.replace("\\","/"))
    if rel.is_absolute() or not rel.parts or any(x in ("",".","..") or x.startswith(".") for x in rel.parts):raise SystemExit("Unsafe output directory")
    root=Path(args.root).expanduser().resolve(strict=True)
    if not root.is_dir() or root.is_symlink():raise SystemExit("Invalid root directory")
    out=(root/rel).resolve()
    if not out.is_relative_to(root) or out==root:raise SystemExit("Output is outside repository")
    pkg=json.loads((root/"package.json").read_text("utf8"))
    if args.script not in pkg.get("scripts",{}):raise SystemExit("Missing npm script")
    git=shutil.which("git")
    npm=shutil.which("npm.cmd") or shutil.which("npm")
    npx=shutil.which("npx.cmd") or shutil.which("npx")
    if not all((git,npm,npx)):raise SystemExit("Git / Node.js/npm/npx required")
    branch=invoke([git,"rev-parse","--abbrev-ref","HEAD"],root,30)
    head=invoke([git,"rev-parse","HEAD"],root,30)
    if branch!=args.branch:raise SystemExit("Checked-out branch mismatch")
    if invoke([git,"status","--porcelain"],root,30):raise SystemExit("Working tree is not clean: commit or stash first")
    target=invoke([git,"ls-remote","origin","refs/heads/"+args.branch],root,35).split()
    if not target or target[0]!=head:raise SystemExit("Local commit is not latest remote branch head")
    print("Verified repository:",root)
    print("Verified branch:",branch,"head:",head)
    print("Pages project:",args.project,"output:",rel)
    if not args.deploy:
        print("Dry run only. Add --deploy to build and upload. No files changed.")
        return
    answer=input("Type DEPLOY to confirm upload for "+args.project+": ").strip()
    if answer!="DEPLOY":raise SystemExit("Cancelled")
    invoke([npm,"run",args.script],root,12*60)
    if not out.is_dir() or not (out/"index.html").is_file():raise SystemExit("Build output missing index.html")
    print(invoke([npx,"--yes","wrangler","pages","deploy",str(rel),"--project-name",args.project,"--branch",args.branch],root,15*60)[-2500:])

if __name__=="__main__":main()
