"""Independent ORIGINAL XML obligations, not a fraction of model-declared slots."""
from tender_harness.docx_ops import engine as e

def source_obligations(original,semantic,inventory):
 _,r=e.b.load_doc(original);body=e.b.tbody(r);stats={'original_blank_cells':0,'declared_original_blank_cells':0,'protected_blank_cells_intentionally_unfilled':0,'merged_continuations_not_fields':0,'original_upload_positions':0,'declared_upload_positions':0,'undeclared':[],'templates':{}}
 for inv in inventory:
  tid=inv['id'];_,s=e.b.load_doc(semantic[tid]);ots=[n for n in list(body)[inv['start']:inv['end']] if n.tag==e.W+'tbl'];sts=[n for n in e.b.tbody(s) if n.tag==e.W+'tbl'];local=[]
  for ti,t in enumerate(ots):
   for ri,row in enumerate(t.findall(e.W+'tr')):
    for ci,c in enumerate(row.findall(e.W+'tc')):
     blank=not e.text(c).strip();upload=all(x in e.text(c) for x in ['上传','预览','删除'])
     if not blank and not upload:continue
     if blank:stats['original_blank_cells']+=1
     if inv['mode'] in ('protected','reference'):
      if blank:stats['protected_blank_cells_intentionally_unfilled']+=1
      continue
     vm=c.find(e.W+'tcPr/'+e.W+'vMerge')
     if blank and vm is not None and vm.get(e.W+'val') in (None,'continue'):
      stats['merged_continuations_not_fields']+=1;continue
     targets=sts if tid=='personnel_resume' else [sts[ti]]
     has=all(q.findall(e.W+'tr')[ri].findall(e.W+'tc')[ci].find('.//'+e.W+'sdt') is not None for q in targets)
     if upload:stats['original_upload_positions']+=1;stats['declared_upload_positions']+=int(has)
     if blank:stats['declared_original_blank_cells']+=int(has)
     if not has:local.append({'template_id':tid,'table':ti,'row':ri,'physical_column':ci,'kind':'upload' if upload else 'blank'})
  stats['templates'][tid]={'uncovered':local};stats['undeclared']+=local
 e.require(not stats['undeclared'],'independent_original_requirement_undeclared');return stats


def source_role_obligations(original,res,cfg):
 import re
 _,root=e.b.load_doc(original);roles={res['records']['assignment:'+p]['content']['role']:p for p in res['selected_people']};found=[];unassigned=[]
 for i,n in enumerate(list(e.b.tbody(root))[:575]):
  t=e.text(n)
  if not re.search(r'安全生产考核合格证(?:书)?[（(][ABC]类[)）]',t):continue
  matched=False
  for role,pid in roles.items():
   m=re.search(re.escape(role)+r'.*?(安全生产考核合格证(?:书)?[（(]([ABC])类[)）])',t)
   if not m:continue
   matched=True;qs=cfg['other_requirements'].get(pid,[]);e.require(any(q['applicable_role']==role and q['title']==m[1] and q['certificate_filter']['equals']['level']==m[2] and q['quote'] in t and int(q['clause'])==i for q in qs),'independent_role_certificate_requirement_undeclared_or_wrong');found.append({'person_id':pid,'role':role,'required_title':m[1],'source_body':i,'level':m[2]})
  if not matched:unassigned.append({'source_body':i,'text':t})
 e.require(not unassigned,'unassigned_original_certificate_requirement');return {'independent_original_role_clause_scan':True,'required':found,'unassigned':unassigned}
