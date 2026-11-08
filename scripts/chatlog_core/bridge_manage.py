"""Owner CLI configuration operations; uses the same save gate as the UI."""
import uuid
from pathlib import Path
from .common import QueryError


def person(config, value):
    rows=[p for p in config['principals'] if p['id']==value]
    if not rows:rows=[p for p in config['principals'] if p['name']==value]
    if len(rows)!=1:
        raise QueryError('person_not_unique','联系人不存在或名字不唯一；请使用 people list 中的 ID',candidates=[dict(id=p['id'],name=p['name']) for p in rows])
    return rows[0]


def manage(bridge,args):
    config=bridge.store.config()
    if args.action in ('pause','resume'):
        config['paused']=args.action=='pause';return bridge.save(config)
    if args.action in ('workspaces','sessions'):
        catalog=bridge.runner(args.backend).catalog()
        if args.action=='workspaces':return catalog.get('workspace_records',catalog['workspaces'])
        return [r for r in catalog['threads'] if not args.workspace or r['workspace']==str(Path(args.workspace).resolve())]
    if args.action=='backend':
        if not bridge.runner(args.backend).running():raise QueryError('backend_unavailable','所选回答后端当前不可用，未切换')
        selected=config['principals'] if args.all else [person(config,args.person)]
        for p in selected:
            target=p.get('targets',{}).get(args.backend)
            if target is None and args.backend=='codex' and p.get('backend','codex')=='codex':target=dict(workspace=p['workspace'],session_id=p['thread_id'])
            if not target:raise QueryError('backend_target_missing','先为联系人绑定该后端的工作空间和会话',person_id=p['id'],backend=args.backend)
            bridge.runner(args.backend).binding(target['session_id'],target['workspace'])
            p.update(backend=args.backend,workspace=target['workspace'],thread_id=target['session_id'])
        return bridge.save(config)
    if args.action=='bind':
        p=person(config,args.person);workspace=str(Path(args.workspace).resolve())
        bridge.runner(args.backend).binding(args.session,workspace)
        targets=dict(p.get('targets') or {})
        if p.get('thread_id'):targets.setdefault(p.get('backend','codex'),dict(workspace=p['workspace'],session_id=p['thread_id']))
        targets[args.backend]=dict(workspace=workspace,session_id=args.session);p['targets']=targets
        if p.get('backend','codex')==args.backend:p.update(workspace=workspace,thread_id=args.session)
        return bridge.save(config)
    if args.action=='people':
        if args.people_action=='list':
            return [dict(id=p['id'],name=p['name'],enabled=p['enabled'],backend=p.get('backend','codex'),workspace=p['workspace'],session_id=p['thread_id'],identities=p['identities'],targets=p.get('targets',{})) for p in config['principals']]
        if args.people_action=='add':
            if args.owner_self:
                if args.wechat or args.wecom:raise QueryError('identity_arguments_conflict','--self 不与其他联系人引用混用')
                identities=[dict(platform=k,account_key=a['account_key'],contact_id=a['self_id'],enabled=True) for k,a in bridge.accounts().items()]
            else:
                refs=[r for r in [args.wechat,args.wecom] if r]
                if not refs:raise QueryError('identity_required','使用 --self 或指定 --wechat/--wecom 精确联系人引用')
                identities=[bridge.feed.resolve_contact(r) for r in refs]
                if any(r and not r.startswith(k+':contact:') for k,r in [('wechat',args.wechat),('wecom',args.wecom)]):raise QueryError('platform_mismatch','联系人引用的平台与参数不一致')
            keys={(i['platform'],i['account_key'],i['contact_id']) for i in identities}
            matches=[p for p in config['principals'] if keys&{(i['platform'],i['account_key'],i['contact_id']) for i in p['identities']}]
            if len(matches)>1:raise QueryError('identities_already_split','这些身份已分配给不同联系人；不能自动合并')
            if matches:
                p=matches[0];existing={(i['platform'],i['account_key'],i['contact_id']) for i in p['identities']}
                if keys<=existing:return dict(id=p['id'],name=p['name'],already_exists=True)
                p['identities'].extend(i for i in identities if (i['platform'],i['account_key'],i['contact_id']) not in existing)
                p['identities_confirmed']=False;p['enabled']=False
            else:
                p=dict(id=uuid.uuid4().hex,name=args.name or ('本人' if args.owner_self else ''),enabled=False,identities_confirmed=False,workspace='',thread_id='',identities=identities)
                config['principals'].append(p)
            bridge.save(config);return dict(id=p['id'],name=p['name'],enabled=False)
        p=person(config,args.person)
        if args.people_action=='remove':config['principals'].remove(p)
        elif args.people_action=='enable':
            if args.confirm_identities:p['identities_confirmed']=True
            p['enabled']=True
        else:p['enabled']=False
        return bridge.save(config)
    raise QueryError('unknown_bridge_command','未知管理命令')
