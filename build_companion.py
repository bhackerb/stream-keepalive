"""Build installable companion directories (no network, no private keys retained)."""
import base64, hashlib, json, shutil, subprocess, tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parent
HOSTS=['streamed.pk','streamed.su','embed.st','rockylive.st','strmd.top','dailywrestling.cc','nightcityv.site','limemint.site']
def extension_id(key):
    digest=hashlib.sha256(base64.b64decode(key)).hexdigest()[:32]
    return ''.join(chr(ord('a')+int(c,16)) for c in digest)
def build():
    keyfile=ROOT/'companion'/'public-key.txt'
    if not keyfile.exists():
        with tempfile.TemporaryDirectory() as tmp:
            private=Path(tmp)/'key.pem'
            subprocess.run(['openssl','genrsa','-out',str(private),'2048'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            public=subprocess.check_output(['openssl','rsa','-in',str(private),'-pubout','-outform','DER'],stderr=subprocess.DEVNULL)
            keyfile.write_text(base64.b64encode(public).decode()+'\n')
    key=keyfile.read_text().strip()
    base={'manifest_version':3,'name':'StreamKeeper Companion','version':'0.2.3','description':'Control only StreamKeeper-owned sports and wrestling windows in your regular browser.',
          'permissions':['nativeMessaging','storage','tabs','alarms','webNavigation'],
          'host_permissions':[f'https://*.{h}/*' for h in HOSTS],
          'content_scripts':[{'matches':[f'https://*.{h}/*' for h in HOSTS],'js':['content.js'],'all_frames':True,'run_at':'document_idle'}]}
    for browser in ('chrome','firefox'):
        target=ROOT/'build'/'companion'/browser;target.mkdir(parents=True,exist_ok=True)
        manifest=dict(base)
        if browser=='chrome':manifest.update(key=key,background={'service_worker':'background.js'})
        else:
            manifest['permissions']=base['permissions']+['sessions']
            manifest.update(background={'scripts':['background.js']},browser_specific_settings={'gecko':{'id':'streamkeeper@bhackerb.local','strict_min_version':'140.0','data_collection_permissions':{'required':['none']}}})
        (target/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
        for file in ('background.js','content.js'):shutil.copy2(ROOT/'companion'/file,target/file)
    shutil.make_archive(str(ROOT/'build'/'streamkeeper-firefox'),'zip',ROOT/'build'/'companion'/'firefox')
    print('Chrome extension ID:',extension_id(key))
if __name__=='__main__':build()
