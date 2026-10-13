"""Install user-scoped native host manifests; never modify browser profiles."""
import argparse
import json
import os
from pathlib import Path
from build_companion import build, extension_id, ROOT
from native_host import NAME, GECKO_ID

def install(configure=False):
    build()
    launcher=ROOT/'build'/'streamkeeper-native-host'
    # Shell single quoting supports spaces and prevents command expansion.
    def quote(value):return "'"+str(value).replace("'", "'\"'\"'")+"'"
    import sys
    launcher.write_text('#!/bin/sh\nexec '+quote(sys.executable)+' '+quote(ROOT/'native_host.py')+' "$@"\n')
    launcher.chmod(0o700)
    base={'name':NAME,'description':'StreamKeeper regular-browser bridge','path':str(launcher),'type':'stdio'}
    cid=extension_id((ROOT/'companion'/'public-key.txt').read_text().strip())
    for directory,allow in [(Path.home()/'.config/google-chrome/NativeMessagingHosts',{'allowed_origins':['chrome-extension://'+cid+'/']}),
                            (Path.home()/'.mozilla/native-messaging-hosts',{'allowed_extensions':[GECKO_ID]})]:
        directory.mkdir(parents=True,exist_ok=True)
        path=directory/(NAME+'.json');path.write_text(json.dumps({**base,**allow},indent=2)+'\n');path.chmod(0o600)
    if configure:
        import yaml
        path=ROOT/'config.yaml';config=yaml.safe_load(path.read_text())
        if not config.get('api',{}).get('api_key') or config['api']['api_key']=='YOUR_API_KEY':raise ValueError('Set API authentication before enabling regular mode')
        backup=path.with_suffix('.yaml.before-regular')
        if not backup.exists():
            fd=os.open(backup,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'wb')as f:f.write(path.read_bytes())
        config.setdefault('api',{})['enabled']=True
        config.setdefault('browser',{}).update(control_mode='regular',engine='firefox')
        path.write_text(yaml.safe_dump(config,sort_keys=False));path.chmod(0o600)
    print('Load Chrome unpacked extension:',ROOT/'build/companion/chrome')
    print('Firefox development manifest:',ROOT/'build/companion/firefox/manifest.json')
    print('Firefox signing archive:',ROOT/'build/streamkeeper-firefox.zip')
if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--enable',action='store_true');args=parser.parse_args();install(args.enable)
