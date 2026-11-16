#!/usr/bin/env python3
"""Independent Longevity Drive archival copy and verified recovery. No source/remote deletion, ever.
No credentials in source code; app_config/token.json is mounted read only.
mode=archive (default): outbound copy-and-verify; never writes the Longevity share.
mode=restore: read-only Drive listing/download of previously archived, content-addressed copies into the five
existing directories; never modifies Drive; never overwrites or replaces an existing local file.
"""
import hashlib, json, os, pathlib, re, sys, tempfile, time, urllib.error, urllib.parse, urllib.request
import mount_guard
VERSION = '0.4.1'

SOURCE = mount_guard.ROOT
DATA = pathlib.Path('/data')
TOKEN = pathlib.Path('/config/token.json')
STATE = DATA / 'archive_state.json'
SCOPE = 'https://www.googleapis.com/auth/drive.file'
ALLOWED = mount_guard.ALLOWED
CHUNK = 8 * 1024 * 1024

class Stop(Exception): pass

def sha_md5(path):
    sha, md5 = hashlib.sha256(), hashlib.md5()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(CHUNK), b''):
            sha.update(chunk); md5.update(chunk)
    return sha.hexdigest(), md5.hexdigest()

def persist(state):
    DATA.mkdir(parents=True,exist_ok=True)
    temp = STATE.with_suffix('.tmp')
    with temp.open('w',encoding='utf8') as f:
        json.dump(state,f,sort_keys=True,indent=2);f.write('\n');f.flush();os.fsync(f.fileno())
    os.chmod(temp,0o600)
    os.replace(temp, STATE)

class Drive:
    def __init__(self):
        token = json.loads(TOKEN.read_text(encoding='utf8'))
        if token.get('scopes') != [SCOPE]: raise Stop('OAuth scope mismatch')
        self.token = token
        self.access = None
    def refresh(self):
        form = urllib.parse.urlencode({'client_id':self.token['client_id'], 'client_secret':self.token['client_secret'],
            'refresh_token':self.token['refresh_token'],'grant_type':'refresh_token'}).encode()
        req=urllib.request.Request('https://oauth2.googleapis.com/token',data=form,headers={'Content-Type':'application/x-www-form-urlencoded'})
        try:
            with urllib.request.urlopen(req,timeout=30) as r: self.access=json.load(r)['access_token']
        except (urllib.error.URLError,KeyError,ValueError) as e:
            raise Stop('OAuth refresh failed; credentials retained; see connectivity and Google consent') from None
    def request(self,method,url,data=None,headers=None,allow308=False):
        if not self.access:self.refresh()
        h={'Authorization':'Bearer '+self.access};h.update(headers or {})
        req=urllib.request.Request(url,data=data,headers=h,method=method)
        try: return urllib.request.urlopen(req,timeout=150)
        except urllib.error.HTTPError as e:
            if allow308 and e.code==308:return e
            # Do not print URL, token, body, or remote file metadata.
            raise Stop(f'Drive request returned HTTP {e.code}; operation {method}') from None
        except urllib.error.URLError:raise Stop('Drive connection unavailable') from None
    def get(self,path,params=None):
        url='https://www.googleapis.com/drive/v3/'+path
        if params:url+='?'+urllib.parse.urlencode(params)
        with self.request('GET',url) as resp:return json.load(resp)
    def folder(self,name,parent=None):
        body={'name':name,'mimeType':'application/vnd.google-apps.folder'}
        if parent:body['parents']=[parent]
        with self.request('POST','https://www.googleapis.com/drive/v3/files?fields=id',json.dumps(body).encode(),
                          {'Content-Type':'application/json'}) as resp:return json.load(resp)['id']
    def metadata(self,fid):
        return self.get('files/'+urllib.parse.quote(fid,safe=''),{'fields':'id,name,size,md5Checksum,mimeType,parents,trashed'})
    def files_named(self,parent,name):
        quote=lambda s:s.replace('\\','\\\\').replace("'","\\'")
        q=f"'{quote(parent)}' in parents and name = '{quote(name)}' and trashed = false"
        found=[];token=None
        while True:
            p={'q':q,'fields':'nextPageToken,files(id,name,size,md5Checksum,trashed)', 'pageSize':'100'}
            if token:p['pageToken']=token
            data=self.get('files',p);found.extend(data.get('files',[]));token=data.get('nextPageToken')
            if not token:return found
    def download_verify(self,fid,sha):
        url='https://www.googleapis.com/drive/v3/files/'+urllib.parse.quote(fid,safe='')+'?alt=media'
        h=hashlib.sha256()
        with self.request('GET',url) as r:
            for block in iter(lambda:r.read(CHUNK),b''):h.update(block)
        return h.hexdigest()==sha
    def list_children(self,parent):
        q=f"'{parent.replace(chr(92),chr(92)*2).replace(chr(39),chr(92)+chr(39))}' in parents and trashed = false"
        found=[];token=None
        while True:
            p={'q':q,'fields':'nextPageToken,files(id,name,size,md5Checksum,mimeType,parents,trashed)','pageSize':'200'}
            if token:p['pageToken']=token
            data=self.get('files',p);found.extend(data.get('files',[]));token=data.get('nextPageToken')
            if not token:return found
    def download_to(self,fid,stream,limit):
        """Stream file content; return (sha256, md5, bytes). Never writes outside the given open stream."""
        url='https://www.googleapis.com/drive/v3/files/'+urllib.parse.quote(fid,safe='')+'?alt=media'
        sha,md5,n=hashlib.sha256(),hashlib.md5(),0
        with self.request('GET',url) as r:
            for block in iter(lambda:r.read(CHUNK),b''):
                n+=len(block)
                if n>limit:raise Stop('Remote object larger than its recorded size; refusing')
                sha.update(block);md5.update(block);stream.write(block)
        return sha.hexdigest(),md5.hexdigest(),n
    def upload(self,path,parent,remote_name):
        size=path.stat().st_size
        body=json.dumps({'name':remote_name,'parents':[parent]}).encode()
        with self.request('POST','https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable&fields=id,size,md5Checksum',body,
            {'Content-Type':'application/json; charset=UTF-8','X-Upload-Content-Type':'application/octet-stream',
             'X-Upload-Content-Length':str(size)}) as response: session=response.headers.get('Location')
        if not session or not session.startswith('https://www.googleapis.com/'):
            raise Stop('Drive resumable session not returned as expected')
        if size==0:
            with self.request('PUT',session,b'',{'Content-Range':'bytes */0','Content-Length':'0'}) as r:
                return json.load(r)['id']
        pos=0
        with path.open('rb') as stream:
            while pos<size:
                stream.seek(pos);block=stream.read(min(CHUNK,size-pos))
                if not block:raise Stop('Source unexpectedly truncated')
                headers={'Content-Type':'application/octet-stream','Content-Length':str(len(block)),
                         'Content-Range':f'bytes {pos}-{pos+len(block)-1}/{size}'}
                # After a network interruption query the server's authoritative Range.
                for attempt in range(4):
                    try:
                        with self.request('PUT',session,block,headers,allow308=True) as r:
                            status=r.code
                            if status in (200,201):return json.load(r)['id']
                            if status!=308:raise Stop('Unexpected upload status')
                            received=r.headers.get('Range')
                        break
                    except Stop:
                        if attempt==3:raise
                        time.sleep(min(2**attempt,8))
                        with self.request('PUT',session,b'',{'Content-Length':'0','Content-Range':f'bytes */{size}'},allow308=True) as check:
                            if check.code in (200,201):return json.load(check)['id']
                            received=check.headers.get('Range')
                        break
                match=re.fullmatch(r'bytes=0-(\d+)',received or '')
                next_pos=int(match.group(1))+1 if match else 0
                if next_pos<=pos:raise Stop('Drive did not acknowledge current chunk; preserving local file')
                if next_pos>size:raise Stop('Invalid Drive upload byte range')
                pos=next_pos
        raise Stop('Drive did not acknowledge upload completion; inspect cloud before retry')

STATUS = SOURCE / '.runtime' / 'bridge_status.json'


def write_status(**fields):
    """Private run-status file outside the five replicated folders: timestamps and result category only."""
    try:
        d = STATUS.parent
        if d.is_symlink(): return
        d.mkdir(mode=0o700, exist_ok=True)
        try: st = json.loads(STATUS.read_text(encoding='utf8'))
        except (OSError, ValueError): st = {}
        st.update(fields); st['version'] = VERSION
        fd, temp = tempfile.mkstemp(prefix='.status_', suffix='.tmp', dir=str(d))
        try:
            with os.fdopen(fd, 'w', encoding='utf8') as f:
                json.dump(st, f, sort_keys=True); f.write('\n'); f.flush(); os.fsync(f.fileno())
            os.replace(temp, STATUS); temp = None
        finally:
            if temp and os.path.exists(temp): os.unlink(temp)
    except OSError:
        pass


def now_iso():
    return time.strftime('%Y-%m-%dT%H:%M:%S+00:00', time.gmtime())


def main():
    rc = run()
    return rc


def run():
    cfg=json.loads((DATA/'options.json').read_text())
    dry=cfg.get('dry_run',True)
    test_only=cfg.get('test_only',True)
    mode=cfg.get('mode','archive')
    if mode not in ('archive','restore'):raise Stop('Unknown operation mode; expected archive or restore')
    try:guard=mount_guard.check()
    except mount_guard.MountGuardError as e:raise Stop(str(e)) from None
    print('Longevity Drive Bridge v'+VERSION+'; mode='+mode+'; dry_run='+str(dry)+'; test_only='+str(test_only))
    print('MOUNT GUARD PASS — external Longevity volume ('+guard['fstype']+')')
    write_status(mode=mode, last_attempt=now_iso(), last_result='running')
    try:
        if mode=='restore':
            rc = restore(cfg,dry)
            write_status(last_result=('restore_dry_run_pass' if dry else 'restore_pass') if rc == 0 else 'restore_partial')
            return rc
        rc = archive(cfg, dry, test_only)
        write_status(**({'last_result': 'dry_run_pass'} if dry else {'last_result': 'archive_pass', 'last_success': now_iso()}))
        return rc
    except (Stop, OSError, ValueError, KeyError, urllib.error.URLError, TypeError):
        write_status(last_result=mode + '_stop')
        raise


def archive(cfg, dry, test_only):
    if dry:
        for folder in ALLOWED:
            paths=[p for p in (SOURCE/folder).iterdir() if p.is_file() and not p.is_symlink()]
            print(folder+': '+str(len(paths))+' top-level candidate files')
        print('DRY RUN PASS — no credentials, Drive access, or changes')
        return
    if not test_only and not cfg.get('production_approved',False):
        raise Stop('Production transfer not enabled: explicit approval option required')
    if test_only:
        marker=SOURCE/'05_Data_Documentation'/'longevity_connectivity_test.txt'
        if not marker.is_file() or marker.is_symlink() or marker.stat().st_size > 4096:
            raise Stop('Missing/invalid small non-sensitive connectivity test file')
    if not TOKEN.is_file():raise Stop('Credential token missing from app-private config folder')
    drive=Drive()
    state=json.loads(STATE.read_text()) if STATE.is_file() else {'schema':1,'root_id':None,'folder_ids':{},'files':{}}
    supplied=cfg.get('root_folder_id','').strip()
    if supplied and state.get('root_id') and state['root_id']!=supplied:raise Stop('Configured root differs from previously verified root')
    root_id=state.get('root_id') or supplied
    if not root_id:
        root_id=drive.folder('Longevity');state['root_id']=root_id;persist(state)
    else:
        m=drive.metadata(root_id)
        if m.get('mimeType')!='application/vnd.google-apps.folder' or m.get('name')!='Longevity' or m.get('trashed'):
            raise Stop('Google Drive root identity mismatch')
        state['root_id']=root_id;persist(state)
    for folder in ALLOWED:
        if test_only and folder != '05_Data_Documentation':continue
        parent=state['folder_ids'].get(folder)
        if parent:
            m=drive.metadata(parent)
            if m.get('mimeType')!='application/vnd.google-apps.folder' or m.get('name')!=folder or root_id not in m.get('parents',[]) or m.get('trashed'):
                raise Stop('Destination folder identity mismatch')
        else:
            existing=drive.files_named(root_id,folder)
            if len(existing)>1:raise Stop('Multiple similarly named Drive folders; manual reconciliation required')
            parent=existing[0]['id'] if existing else drive.folder(folder,root_id)
            state['folder_ids'][folder]=parent;persist(state)
        for path in sorted((SOURCE/folder).iterdir()):
            if test_only and path.name != 'longevity_connectivity_test.txt':continue
            if not path.is_file() or path.is_symlink():continue
            if any(x in path.name.lower() for x in ('token','credential','secret','.env','password')):continue
            before=path.stat();sha,md5=sha_md5(path);after=path.stat()
            if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):raise Stop('Source changed during hashing')
            key=folder+'/'+path.name
            # Content-addressed remote name enables idempotent reconciliation after restart or state loss.
            remote_name=path.name+'.sha256-'+sha
            previous=state['files'].get(key)
            if previous and previous.get('sha256')==sha:
                try:
                    m=drive.metadata(previous['id'])
                    if str(m.get('size'))==str(before.st_size) and m.get('md5Checksum')==md5 and not m.get('trashed'):
                        print('Already archived: '+key);continue
                except Stop:pass
            matches=drive.files_named(parent,remote_name)
            verified=[]
            for match in matches:
                if str(match.get('size'))==str(before.st_size) and match.get('md5Checksum')==md5 and drive.download_verify(match['id'],sha):
                    verified.append(match['id'])
            if verified:
                state['files'][key]={'id':verified[0],'sha256':sha,'size':before.st_size,'md5':md5};persist(state)
                print('Reconciled existing verified copy: '+key);continue
            if matches:raise Stop('Remote object with expected name failed integrity; refusing duplicate upload')
            fid=drive.upload(path,parent,remote_name)
            m=drive.metadata(fid)
            if str(m.get('size'))!=str(before.st_size) or m.get('md5Checksum')!=md5 or not drive.download_verify(fid,sha):
                raise Stop('Remote integrity failed; local original retained; review remote object')
            state['files'][key]={'id':fid,'sha256':sha,'size':before.st_size,'md5':md5};persist(state)
            print('Verified upload: '+key)
    print('ARCHIVE PASS — existing source files preserved')
    return 0


CA = re.compile(r'^(?P<orig>.+)\.sha256-(?P<h>[0-9a-f]{64})$')
PIN_SPEC = re.compile(r'^(0[1-5]_[A-Za-z_]+)/([A-Za-z0-9][A-Za-z0-9._ ()+-]{0,200})=([0-9a-f]{64})$')


def parse_pins(entries):
    """Owner-recorded content addresses (private runtime option 'pinned_hashes', entries 'folder/name=sha256')."""
    pins = {}
    for e in entries or []:
        m = PIN_SPEC.match(str(e).strip())
        if not m or m.group(1) not in ALLOWED or (m.group(1) + '/' + m.group(2)) in pins:
            raise Stop('Invalid or duplicate pinned_hashes option entry')
        pins[m.group(1) + '/' + m.group(2)] = m.group(3)
    return pins
SAFE_NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._ ()+-]{0,200}$')


def local_sha(path):
    return sha_md5(path)[0]


def place(drive, item, folder_dir, name, size, md5, sha):
    """Download into a private temp file in the destination directory, verify, then claim the name without
    ever replacing an existing file. Returns 'restored' or raises Stop."""
    fd, temp = tempfile.mkstemp(prefix='.restore_', suffix='.part', dir=str(folder_dir))
    try:
        with os.fdopen(fd, 'wb') as out:
            got_sha, got_md5, n = drive.download_to(item['id'], out, size)
            out.flush(); os.fsync(out.fileno())
        if n != size or got_md5 != md5 or got_sha != sha:
            raise Stop('Downloaded content failed size/MD5/SHA-256 verification; nothing placed')
        target = folder_dir / name
        try:
            os.link(temp, target)
        except FileExistsError:
            raise Stop('Destination appeared during restore; refusing to replace it') from None
        except OSError:
            raise Stop('Filesystem cannot guarantee atomic no-overwrite placement; original preserved') from None
        if local_sha(target) != sha:
            raise Stop('Restored file failed post-placement verification')
        return 'restored'
    finally:
        if temp and os.path.exists(temp): os.unlink(temp)


def plan_folder(folder, children, recorded, pins=None):
    """Pure decision step (no I/O): map each original name to exactly one verified remote copy, or a refusal."""
    remote = {}
    for c in children:
        m = CA.match(c.get('name', ''))
        if c.get('trashed') or c.get('mimeType') == 'application/vnd.google-apps.folder':
            continue
        if not m:
            remote.setdefault(None, []).append(c); continue
        remote.setdefault(m.group('orig'), []).append((m.group('h'), c))
    names = {k.split('/', 1)[1] for k in recorded if k.split('/', 1)[0] == folder} | {k for k in remote if k}
    plan = {}
    for name in sorted(names):
        key = folder + '/' + name
        if not SAFE_NAME.match(name) or any(x in name.lower() for x in ('token', 'credential', 'secret', '.env', 'password')):
            plan[name] = ('refused_unsafe_name', None); continue
        copies = remote.get(name, [])
        rec = recorded.get(key)
        if rec:
            want = rec.get('sha256')
            hits = [c for h, c in copies if h == want and c.get('id') == rec.get('id')]
            if len(hits) != 1:
                plan[name] = ('recorded_copy_missing', None); continue
            item = hits[0]
            if str(item.get('size')) != str(rec.get('size')) or item.get('md5Checksum') != rec.get('md5'):
                plan[name] = ('recorded_copy_metadata_mismatch', None); continue
            choice = (item, int(rec['size']), rec['md5'], want)
        else:
            distinct = {h for h, _ in copies}
            if len(distinct) != 1 or len(copies) != 1:
                plan[name] = ('ambiguous_multiple_versions', None); continue
            h, item = copies[0]
            if not str(item.get('size', '')).isdigit() or not item.get('md5Checksum'):
                plan[name] = ('remote_metadata_incomplete', None); continue
            choice = (item, int(item['size']), item['md5Checksum'], h)
        pin = (pins or {}).get(key)
        if pin and choice[3] != pin:
            plan[name] = ('refused_pinned_hash_mismatch', None); continue
        plan[name] = ('candidate', choice)
    return plan, len(remote.get(None, []))


def restore(cfg, dry):
    if not dry and not cfg.get('restore_approved', False):
        raise Stop('Restore not enabled: explicit restore_approved option required')
    if not TOKEN.is_file(): raise Stop('Credential token missing from app-private config folder')
    if not STATE.is_file(): raise Stop('No verified archive state; recorded remote copies cannot be identified')
    state = json.loads(STATE.read_text())
    drive = Drive()
    root_id = state.get('root_id')
    supplied = cfg.get('root_folder_id', '').strip()
    if not root_id or (supplied and supplied != root_id): raise Stop('Configured root differs from previously verified root')
    m = drive.metadata(root_id)
    if m.get('mimeType') != 'application/vnd.google-apps.folder' or m.get('name') != 'Longevity' or m.get('trashed'):
        raise Stop('Google Drive root identity mismatch')
    totals = {}
    for folder in ALLOWED:
        parent = state.get('folder_ids', {}).get(folder)
        if not parent:
            totals['folder_not_archived'] = totals.get('folder_not_archived', 0) + 1; continue
        fm = drive.metadata(parent)
        if fm.get('mimeType') != 'application/vnd.google-apps.folder' or fm.get('name') != folder or root_id not in fm.get('parents', []) or fm.get('trashed'):
            raise Stop('Destination folder identity mismatch')
        plan, unaddressed = plan_folder(folder, drive.list_children(parent), state.get('files', {}), parse_pins(cfg.get('pinned_hashes')))
        if unaddressed: totals['remote_not_content_addressed'] = totals.get('remote_not_content_addressed', 0) + unaddressed
        counts = {}
        for name, (status, choice) in plan.items():
            if status == 'candidate':
                item, size, md5, sha = choice
                target = SOURCE / folder / name
                if target.is_symlink():
                    status = 'conflict_local_symlink'
                elif target.exists():
                    status = 'present_verified' if target.is_file() and local_sha(target) == sha else 'conflict_existing_differs'
                elif dry:
                    status = 'would_restore'
                else:
                    status = place(drive, item, SOURCE / folder, name, size, md5, sha)
            counts[status] = counts.get(status, 0) + 1
            totals[status] = totals.get(status, 0) + 1
        print('RESTORE ' + folder + ': ' + ', '.join(f'{k}={v}' for k, v in sorted(counts.items())))
    bad = {k: v for k, v in totals.items() if k not in ('restored', 'present_verified', 'would_restore', 'folder_not_archived')}
    print('RESTORE TOTALS: ' + ', '.join(f'{k}={v}' for k, v in sorted(totals.items())))
    if bad:
        print('RESTORE PARTIAL — refused or unverifiable items left untouched; no Drive changes; no overwrites')
        return 2
    print(('RESTORE DRY RUN PASS' if dry else 'RESTORE PASS') + ' — no Drive changes; no overwrites')
    return 0

if __name__=='__main__':
    try:sys.exit(main() or 0)
    except (Stop,OSError,ValueError,KeyError,urllib.error.URLError,TypeError) as exc:
        print('ARCHIVE STOP: '+str(exc),file=sys.stderr);sys.exit(1)