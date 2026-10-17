#!/usr/bin/env zsh
# Exercise the real raw launcher/auto entry against a fake native binary.
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
python3 -B - "$ROOT" <<'PYTEST'
from pathlib import Path
import json, os, shlex, subprocess, sys, tempfile
repo=Path(sys.argv[1])
sys.path.insert(0,str(repo/'bin'))
from harness_auto import _claude_launcher_args, ResolutionError
owner='aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
for tail,expected in [
    ([],['claude']),
    (['--cwd','claude','--isolated-session',owner,'claude','--','text'],['--cwd','claude','--isolated-session',owner,'claude','claude','--','text']),
    (['--cwd','','--no-isolated','','--','text'],['--cwd','','--no-isolated','claude','','--','text']),
    (['--isolated','claude'],['--isolated','claude','claude']),
    (['--model','claude','--cwd','value'],['claude','--model','claude','--cwd','value']),
    (['--','--cwd','DIR','--isolated-session',owner],['claude','--','--cwd','DIR','--isolated-session',owner]),
    (['--passthrough','--cwd','DIR'],['claude','--passthrough','--cwd','DIR']),
    (['','--cwd','DIR'],['claude','','--cwd','DIR']),
]:
    original=tail.copy()
    assert _claude_launcher_args(tail)==expected,(tail,expected)
    assert tail==original,'auto adapter mutated caller argv'
for tail in [['--cwd'],['--isolated-session'],['--cwd','DIR','--isolated-session']]:
    try:_claude_launcher_args(tail)
    except ResolutionError:pass
    else:raise AssertionError(('auto accepted a missing outer-control value',tail))
print('PASS: auto literal prefix/value/empty/prompt matrix and missing-value refusal')
with tempfile.TemporaryDirectory(prefix='explicit-claude-') as tmp:
    base=Path(tmp);home=base/'home';binary=base/'bin';profile=home/'profile with spaces';log=base/'native.json'
    for path in (binary,profile/'config',profile/'.claude',home/'.claude',home/'.config/harness-launcher/profiles'):
        path.mkdir(parents=True,exist_ok=True)
    (profile/'config/launcher.env').write_text('HARNESS_NAME="fixture"\nHARNESS_PREFIX="fixture"\nHARNESS_SESSION_ISOLATION_DEFAULT=0\n')
    (profile/'.mcp.json').write_text('{"mcpServers":{}}\n')
    (home/'.config/harness-launcher/profiles/fixture').write_text(str(profile)+'\n')
    (binary/'claude').write_text('#!/usr/bin/python3\nimport json,os,sys\nwith open(os.environ["FIXTURE_LOG"],"w") as f:json.dump({"argv":sys.argv[1:],"cwd":os.getcwd()},f)\n')
    (binary/'uuidgen').write_text('#!/bin/sh\nprintf "%s\\n" AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA\n')
    # Never enter a real interactive composer on a failing management route.
    (binary/'gum').write_text('#!/bin/sh\nexit 90\n')
    for path in binary.iterdir():path.chmod(0o700)
    env={'HOME':str(home),'PATH':str(binary)+':/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin','TMPDIR':str(base),'CLAUDE_CONFIG_DIR':str(home/'.claude'),'HARNESS_CLAUDE_PLUGIN_MIRROR':'0','FIXTURE_LOG':str(log),'PYTHONDONTWRITEBYTECODE':'1','HARNESS_POST_EDIT_CODEX_SYNC':'0','HARNESS_POST_COMMIT_CODEX_SYNC':'0','TERM':'dumb','LC_ALL':'C'}
    def run(args,auto=False):
        log.unlink(missing_ok=True)
        if auto:
            command=[sys.executable,'-B',str(repo/'bin/harness_auto.py'),*args]
        else:
            command=['/bin/zsh','-f','-c','source "$1/bin/aliases.zsh"; shift; _harness_launcher_run "$@"','test',str(repo),str(profile),*args]
        done=subprocess.run(command,cwd=profile,env=env,input=b'',capture_output=True,timeout=20)
        assert done.returncode==0,(args,done.returncode,done.stderr.decode())
        assert log.exists(),('native was not called',args)
        return json.loads(log.read_text())
    def settings_checked(argv):
        assert argv.count('--settings')==1,argv
        settings=json.loads(argv[argv.index('--settings')+1])
        hook=settings['hooks']['SessionStart'][0]['hooks'][0]['command']
        command=shlex.split(hook)
        assert command[1:3]==[str(repo/'bin/harness-launch-record'),'claude'],command
        assert command[command.index('--source-root')+1]==str(profile.resolve()),command
        assert command[command.index('--isolated')+1]=='0',command
        assert settings['permissions']['ask'],settings
    implicit=run(['base','--','prompt'])
    explicit=run(['claude','base','--','prompt'])
    settings_checked(explicit['argv'])
    assert explicit==implicit,('explicit selector changes native argv',explicit,implicit)
    assert explicit['argv'].count('claude')==0,explicit
    assert explicit['argv'][:4]==['--model','sonnet','--effort','high'],explicit
    print('PASS: explicit Claude base equals implicit complete native argv')
    for tail in [
        ['claude','--','text'],
        ['base','--','','a b','claude','--resume','resume','--passthrough'],
        ['base','--passthrough','--model','claude','--permission-mode','plan','--','','a b'],
        ['--passthrough','claude','--','text'],
        ['--','claude','--cwd','literal'],
    ]:
        # A raw leading claude is now a selector, so use a selector-free
        # reference and independently add the expected literal native word.
        if tail[0]=='claude':
            legacy=run(tail[1:])
            legacy['argv'].insert(0,'claude')
        else:
            legacy=run(tail)
        selected=run(['claude',*tail])
        assert selected==legacy,('caller tokens changed',tail,selected,legacy)
        if tail==['claude','--','text']:
            assert selected['argv'].count('claude')==1,selected
            assert selected['argv'][-2:]==['--','text'],selected
        if '--model' in tail:
            assert selected['argv'].count('--model')==1,selected
            assert selected['argv'][selected['argv'].index('--model')+1]=='claude',selected
            assert selected['argv'].count('--permission-mode')==1,selected
        print('PASS: literal/prompt/value/passthrough token parity',repr(tail))
    assert run(['claude','claude','--','text'],auto=True)==run(['claude','claude','--','text']), 'auto consumed caller claude'
    assert run(['claude','--cwd',str(profile),'--no-isolated','claude','--','text'],auto=True)==run(['--cwd',str(profile),'--no-isolated','claude','claude','--','text']), 'auto misplaced outer controls'
    print('PASS: real standalone auto preserves one caller claude and cwd/control order')
    for native in [['auth','status'],['doctor'],['mcp','list'],['mcp','--passthrough','auth'],['plugin','list'],['help'],['--help'],['--version']]:
        for prefix in [[],['--no-isolated'],['--isolated-session','aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa']]:
            result=run([*prefix,'claude',*native])
            assert result['argv']==native,('management injected session/model/MCP/settings',prefix,native,result)
        print('PASS: native management has exact argv',native)
    for args in [['claude','codex','--','text'],['claude','kiro-cli','--','text']]:
        done=subprocess.run(['/bin/zsh','-f','-c','source "$1/bin/aliases.zsh"; shift; _harness_launcher_run "$@"','test',str(repo),str(profile),*args],cwd=profile,env=env,input=b'',capture_output=True,timeout=20)
        assert done.returncode==2,('explicit Claude silently switched runtime',args,done.returncode)
    print('PASS: explicit Claude cannot switch to another provider prefix')
query=subprocess.run(['/bin/bash','--noprofile','--norc','-c','. "$1/harness-common.sh"; harness_launcher_raw_claude_contract','_',str(repo/'bin')],env={'PATH':'/usr/bin:/bin','LC_ALL':'C'},capture_output=True)
assert (query.returncode,query.stdout,query.stderr)==(0,b'raw-claude-v1\n',b''),(query.returncode,query.stdout,query.stderr)
print('PASS: source-only raw Claude argv contract is exact and pure')
PYTEST
