import argparse
import getpass
from app import db, set_user

p=argparse.ArgumentParser(description='Manage Fusion Cloud accounts locally on the VPS')
p.add_argument('action',choices=['add-user','set-password','revoke','disable','enable','list-users'])
p.add_argument('username',nargs='?')
args=p.parse_args()
if args.action=='list-users':
    with db() as c:
        for row in c.execute('SELECT name,disabled,device FROM users ORDER BY name'):
            print(row['name'], 'disabled' if row['disabled'] else 'enabled', row['device'] or '')
elif not args.username:
    p.error('username is required')
elif args.action in ('add-user','set-password'):
    password=getpass.getpass('Password (12+ characters): ')
    if password!=getpass.getpass('Repeat password: '):
        raise SystemExit('Passwords differ')
    set_user(args.username,password,reset=args.action=='set-password')
    print('Account saved; previous session revoked if password was reset.')
else:
    with db(True) as c:
        name=args.username.lower()
        if args.action=='enable':
            count=c.execute('UPDATE users SET disabled=0 WHERE name=?',(name,)).rowcount
        elif args.action=='disable':
            count=c.execute('UPDATE users SET disabled=1,token=NULL,expires=NULL WHERE name=?',(name,)).rowcount
        else:
            count=c.execute('UPDATE users SET token=NULL,expires=NULL WHERE name=?',(name,)).rowcount
        if not count:raise SystemExit('Account not found')
    print('Done')
