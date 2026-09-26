#!/usr/bin/env python3
"""Public structural checker; not a correctness oracle."""
import json, sys
x=json.load(open(sys.argv[1]))
assert set(x)=={'query_id','settlements','trace','exceptions'}
assert isinstance(x['query_id'],str)
schemas={
 'settlements': {'date','merchant_id','region','tier','net_usd_micro','entry_count'},
 'trace': {'entry_id','version','date','merchant_id','region','tier','usd_micro','customer_token'},
 'exceptions': {'stream','key','code','version'}}
for name, fields in schemas.items():
 assert isinstance(x[name],list)
 for row in x[name]:
  assert set(row)==fields, (name,row)
  for k in ('version','net_usd_micro','entry_count','usd_micro'):
   if k in row and row[k] is not None: assert type(row[k]) is int, (k,row[k])
assert x['settlements']==sorted(x['settlements'],key=lambda r:(r['date'],r['merchant_id'],r['region'],r['tier']))
assert x['trace']==sorted(x['trace'],key=lambda r:r['entry_id'])
assert x['exceptions']==sorted(x['exceptions'],key=lambda r:(r['stream'],r['key'],r['code'],-1 if r['version'] is None else r['version']))
print('Output structure and ordering OK; values not checked.')
