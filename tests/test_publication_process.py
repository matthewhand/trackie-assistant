"""Real process-death recovery of the production Linux prepared-group publisher."""

import json
import subprocess
import sys

import pytest

from suno_dl.publication import publish_prepared
from tests.test_publication import v2_group


@pytest.mark.parametrize(
    "phase",
    [
        "before_link_1",
        "before_link_2",
        "after_link_2",
        "after_source_1",
        "after_source_2",
        "after_rmdir",
        "before_marker_unlink",
    ],
)
def test_killed_publisher_retains_recoverable_ownership(tmp_path, phase):
    marker, output = v2_group(tmp_path)
    expected = json.loads(marker.read_text())
    script = """
import os,sys,signal
from pathlib import Path
from suno_dl.publication import publish_prepared
root,marker=map(Path,sys.argv[1:3]);phase=sys.argv[3]
real_link,real_unlink,real_rmdir=os.link,os.unlink,os.rmdir
links=removed=0
def stop():
 os.kill(os.getpid(),signal.SIGKILL)
def link(*args,**kwargs):
 global links
 links+=1
 if phase=="before_link_"+str(links):stop()
 result=real_link(*args,**kwargs)
 if phase=="after_link_"+str(links):stop()
 return result
def unlink(path,*args,**kwargs):
 global removed
 if Path(path)==marker and phase=="before_marker_unlink":stop()
 result=real_unlink(path,*args,**kwargs)
 if ".suno-staging" in str(path):
  removed+=1
  if phase=="after_source_"+str(removed):stop()
 return result
def rmdir(path,*args,**kwargs):
 result=real_rmdir(path,*args,**kwargs)
 if phase=="after_rmdir":stop()
 return result
os.link,os.unlink,os.rmdir=link,unlink,rmdir
publish_prepared(root,marker)
"""
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path), str(marker), phase])
    assert result.returncode == -9 and marker.exists()
    assert publish_prepared(tmp_path, marker) == expected["id"]
    assert not marker.exists()
    assert len(list(output.glob("*.m4a"))) == len(list(output.glob("*.json"))) == 1
    assert all(path.stat().st_nlink == 1 for path in output.iterdir())
