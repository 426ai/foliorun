"""Full response formats: constrained original DOCX extraction/semantic compilation/slot-only fill.
LIVE writing runs inside the framework sandbox. Host calls are explicitly OFFLINE tests only.
"""
from pathlib import Path
import json,hashlib,zipfile,copy,re,os,io,decimal,datetime,argparse,sys
from tender_harness.docx_ops import base as b
from docx import Document
from docx.shared import Inches
from lxml import etree
W=b.W;A=b.A;R=b.R;require=b.require;sha=b.sha;dh=b.dh;text=b.text;canon=b.canon
IDS_ORDER=['id_card','diploma','practice_cert','title_certificate','other_cert','social_security','labor_contract','performance']
LABELS=['身份证','学历学位证','执业资格证书','职称证书','其他证书','社保证明','劳动合同','业绩证明材料']
def read(p):return json.loads(Path(p).read_text())
def write(p,d):Path(p).write_text(json.dumps(d,ensure_ascii=False,indent=2))
def unpack(p,dest):
 h=sha(p);target=Path(dest)/h
 with zipfile.ZipFile(p) as z:
  m=json.loads(z.read('bundle-manifest.json'));require(set(z.namelist())==set(m['files'])|{'bundle-manifest.json'},'bundle_members')
  target.mkdir(parents=True,exist_ok=True)
  for name,digest in m['files'].items():
   require(not name.startswith(('/','.')) and '..' not in Path(name).parts and '.env' not in name and '\\' not in name,'unsafe_bundle_member');data=z.read(name);require(hashlib.sha256(data).hexdigest()==digest,'bundle_hash');q=target/name
   if q.exists():require(q.read_bytes()==data,'frozen_cache_changed')
   else:q.parent.mkdir(parents=True,exist_ok=True);q.write_bytes(data);q.chmod(0o444)
 return target

def normalize(root):
 repairs=b.normalize_ppr_order(root);seq='tblStyle tblpPr tblOverlap bidiVisual tblStyleRowBandSize tblStyleColBandSize tblW jc tblCellSpacing tblInd tblBorders shd tblLayout tblCellMar tblLook tblCaption tblDescription tblPrChange'.split();rank={W+n:i for i,n in enumerate(seq)}
 for i,p in enumerate(root.iter(W+'tblPr')):
  old=list(p);require(all(n.tag in rank for n in old),'unknown_tblPr');new=sorted(old,key=lambda n:rank[n.tag])
  if old!=new:
   before=sorted(canon(n) for n in old)
   for n in old:p.remove(n)
   for n in new:p.append(n)
   require(before==sorted(canon(n) for n in p),'style_value_changed');repairs.append({'kind':'CT_TblPr_order','index':i,'property_values_unchanged':True})
 for n in root.iter(W+'noWrap'):
  if n.get(W+'val') in ('0','1','false','true'):
   old=n.get(W+'val');n.set(W+'val','off' if old in ('0','false') else 'on');repairs.append({'kind':'equivalent_ST_OnOff_lexical','element':'noWrap','before':old,'after':n.get(W+'val'),'logical_value_unchanged':True})
 return repairs

def blanknode(n):return not text(n).strip()
def slot(rule,tid,locator,owner=None,kind='text',**data):
 key=tid+'|'+rule['id']+'|'+str(owner or '')+'|'+json.dumps(locator,sort_keys=True);return {'tag':'full:'+hashlib.sha256(key.encode()).hexdigest()[:28],'template_id':tid,'rule_id':rule['id'],'label':rule['label'],'kind':kind,'owner_id':owner,'locator':locator,'mandatory_declaration':True,**data}
def binding(rule,res):
 keys=rule.get('record_keys') or ([rule['record_key']] if rule.get('record_key') else []);refs=[]
 for key in keys:
  require(key in res['records'],'unknown_source_record');r=res['records'][key];require(r['sha256']==dh(r['content']) and r['version']==1,'source_record_version_hash');refs.append({'key':key,'id':r['id'],'version':r['version'],'sha256':r['sha256']})
 return refs

def rule_owner(rule,res):
 code=rule.get('binding')
 if code in res.get('identity_bindings',{}):
  x=res['identity_bindings'][code];return x['owner_type'],x['owner_id'],x['fixture_side']
 refs=binding(rule,res)
 if len(refs)==1:
  x=res['records'][refs[0]['key']];return x['kind'],x['id'],None
 return ('composite' if refs else None),None,None

def resolve(s,cfg,res):
 if s.get('origin')=='unassigned_original_row':
  require(any(p['body']==s['locator']['body'] and p.get('unused_cells_policy')=='semantic_labels_all_original_blanks' for p in cfg['table_policies']),'unused_row_rule_missing');return None,'no_frozen_record_assigned_to_preserved_original_row'
 if s.get('origin')=='base':return b.resolve(s['base_slot'],cfg['base_contract'],res)
 rule=next((r for r in cfg.get('rules',[]) if r['id']==s['rule_id']),None)
 if rule is None and s.get('origin')=='resume':return resume_value(s,res)
 if rule is None:return None,'rule_missing'
 require((s.get('owner_type'),s.get('owner_id'),s.get('identity_side'))==rule_owner(rule,res),'declared_owner_id_type_conflict');require(rule['mode']==s['mode'] and rule.get('path')==s.get('path') and rule['transform']==s['transform'],'field_mapping_conflict')
 if s['mode']=='abstain':return None,'bidder_decision_narrative_or_unassigned'
 require(s['source_refs']==binding(rule,res),'source_id_version_hash');values=[]
 for ref in s['source_refs']:
  value=res['records'][ref['key']]['content']
  for p in (rule.get('path') or '').split('.'):value=value.get(p) if isinstance(value,dict) else None
  if value is None or value=='':return None,'source_value_missing'
  require(isinstance(value,(str,int,float)) and not isinstance(value,bool),'source_type_mismatch')
  if rule['transform']=='money_wan':
   require(isinstance(value,(int,float)),'financial_money_type');value=format(decimal.Decimal(str(value))/decimal.Decimal(10000),'f').rstrip('0').rstrip('.') if '.' in format(decimal.Decimal(str(value))/decimal.Decimal(10000),'f') else str(int(decimal.Decimal(str(value))/10000))
  values.append(str(value))
 return ('；'.join(values),'ok') if values else (None,'no_frozen_source')

def resume_refs(pid,path,res):
 r=res['records']['person:'+pid];refs=binding({'record_key':'person:'+pid},res)
 if path=='role':refs+=binding({'record_key':'assignment:'+pid},res)
 elif path.startswith('experience:'):
  _,i,_=path.split(':');xs=[x for x in res['native']['material_person_experiences'] if x['person_id']==r['id'] and not x.get('deleted_at')]
  if int(i)<len(xs):
   x=xs[int(i)];refs.append({'key':'native:material_person_experiences:'+x['id'],'id':x['id'],'version':1,'sha256':dh(x)})
 return refs

def resume_value(s,res):
 pid=s['person_alias'];r=res['records']['person:'+pid];path=s.get('path');require(s['source_refs']==resume_refs(pid,path,res),'resume_source_version_hash')
 if s.get('manual'):return None,'source_value_missing_or_narrative'
 if path=='role':value=res['records']['assignment:'+pid]['content']['role']
 elif path.startswith('experience:'):
  _,index,field=path.split(':');xs=[x for x in res['native']['material_person_experiences'] if x['person_id']==r['id'] and not x.get('deleted_at')]
  if int(index)>=len(xs):return None,'optional_experience_row_missing'
  x=xs[int(index)];value=(x.get('start_on','')+'—'+x.get('end_on','')) if field=='dates' else x.get(field)
 else:value=r['content'].get(path)
 if value is None or value=='':return None,'source_value_missing'
 require(isinstance(value,(str,int,float)) and not isinstance(value,bool),'source_type_mismatch');return str(value),'ok'

def current(a):return a.get('is_current') is True and not a.get('deleted_at') and not a.get('superseded_by_id')
def eligible_attachment(res,pid,category,cfg,other):
 p=res['records']['person:'+pid]['id'];ats=list(res['attachments'].values());certs=[c for c in res['native']['material_certificates'] if c['person_id']==p and not c.get('deleted_at')];ref=res['reference']['date']
 def certgood(c):return (not c.get('expires_on') or c['expires_on']>=ref) and (not c.get('issued_on') or c['issued_on']<=ref)
 if category in ('id_card','diploma','title_certificate','labor_contract'):
  cat='other' if category=='labor_contract' else category;xs=[a for a in ats if a['owner_type']=='person' and a['owner_id']==p and a['category']==cat and (category!='labor_contract' or a.get('poc_doc_type')=='labor_contract')]
 elif category in ('practice_cert','other_cert'):
  if category=='other_cert':
   req=cfg['other_requirements'].get(pid,[])
   if not req:return [],'requirement_unknown'
   chosen=other[pid]['selected_certificate_ids'];f=req[0]['certificate_filter'];allowed=[c for c in certs if f.get('name_contains','') in c['name'] and all(c.get(k)==v for k,v in f.get('equals',{}).items()) and certgood(c)];require(set(chosen)<=set(c['id'] for c in allowed),'wrong_other_certificate_source_or_owner');require(bool(chosen)==bool(allowed),'supported_other_certificate_omitted');cs=[c for c in allowed if c['id'] in chosen]
  else:
   cs=[c for c in certs if c.get('poc_kind')=='执业资格' and certgood(c)]
   filters=cfg.get('practice_filters',{}).get(res['records']['assignment:'+pid]['content']['role'],{}).get('equals',{});cs=[c for c in cs if all(c.get(k)==v for k,v in filters.items())]
  ids={c['id'] for c in cs};xs=[a for a in ats if a['owner_type']=='person_certificate' and a['owner_id'] in ids and a['category']=='certificate_file']
 elif category=='social_security':
  ids={m['proof_id'] for m in res['native']['material_social_insurance_persons'] if m['person_id']==p};proofs={q['id']:q for q in res['native']['material_social_insurance_proofs']};ids={i for i in ids if proofs[i].get('issued_on','')<=ref};xs=[a for a in ats if a['owner_type']=='social_insurance_proof' and a['owner_id'] in ids and a['category']=='social_security']
 else:
  # Contract evidence is sufficient for the named project manager only. A generic contract is NOT a design/construction appointment letter.
  role=res['records']['assignment:'+pid]['content']['role']
  if cfg['performance_evidence_by_role'].get(role,{}).get('kind')!='managed_project_contract':return [],'role_specific_appointment_proof_unavailable'
  projects={q['id'] for q in res['native']['material_performances'] if q.get('manager_person_uuid')==p};xs=[a for a in ats if a['owner_type']=='performance' and a['owner_id'] in projects and a['category']=='contract']
 valid=[a for a in xs if current(a)];return valid,'ok' if valid else 'required_evidence_missing_or_date_conflict'

def image_binding(s,res,cfg,other):
 if s.get('origin')=='base':
  q=s['base_slot'];a=res['images'][q['fixture']];require((a['owner_type'],a['owner_id'],a['category'],a['poc_identity_side'],a['sha256'])==(q['owner_type'],q['owner_id'],q['category'],q['side'],q['sha256']),'wrong_legal_owner_side');return [res['attachments'][a['id']]],'ok'
 if s.get('origin')=='resume':
  xs,why=eligible_attachment(res,s['person_alias'],s['category'],cfg,other);require([a['id'] for a in xs]==s['attachment_ids'],'attachment_selection_changed');return xs,why
 if s.get('origin')=='unassigned_original_row':return [],'no_frozen_record_assigned_to_original_attachment'
 rule=next((r for r in cfg['rules'] if r['id']==s['rule_id']),None)
 if rule is None:return [],'rule_missing'
 require((s.get('owner_type'),s.get('owner_id'),s.get('identity_side'))==rule_owner(rule,res),'declared_identity_owner_or_side_conflict');code=rule.get('binding')
 if not code:return [],'frozen_binary_unavailable'
 bind=res['identity_bindings'][code];a=res['attachments'][bind['attachment_id']];require((a['id'],a['owner_type'],a['owner_id'],a['sha256'],a.get('poc_identity_side'))==(bind['attachment_id'],bind['owner_type'],bind['owner_id'],bind['sha256'],bind['fixture_side']),'identity_wrong_owner_side');require(code.startswith('legal')==(a['owner_type']=='legal_representative'),'legal_agent_confusion');return [a],'ok'

def verify_image(a,res,root):
 require(current(a),'image_not_current');r=res['attachments'].get(a['id']);require(r==a,'image_source_hash_version');p=Path(root)/'inputs/attachments'/Path(a['virtual_path']).name;require(p.is_file() and sha(p)==a['sha256'],'image_file_hash');return p

def inject(p,ops):
 # Preserve non-text run children (notably source page breaks), never drop them to make a slot fit.
 total=text(p);atoms=[];pos=0
 for child in p:
  if child.tag==W+'pPr':continue
  if child.tag!=W+'r':atoms.append((pos,pos,copy.deepcopy(child),None));continue
  for item in child:
   if item.tag==W+'rPr':continue
   run=copy.deepcopy(child)
   for n in list(run):
    if n.tag!=W+'rPr':run.remove(n)
   run.append(copy.deepcopy(item))
   if item.tag==W+'t':
    value=item.text or '';atoms.append((pos,pos+len(value),run,value));pos+=len(value)
   else:atoms.append((pos,pos,run,None))
 require(pos==len(total),'text_projection_mismatch');ordered=sorted(ops,key=lambda x:x['start']);cursor=0;new=[]
 def fixed(a,z,final=False):
  for x,y,n,value in atoms:
   if value is None:
    if a<=x<z or (final and x==z):new.append(copy.deepcopy(n))
   else:
    lo=max(a,x);hi=min(z,y)
    if lo<hi:
     q=copy.deepcopy(n);t=q.find(W+'t');t.text=value[lo-x:hi-x];t.set('{http://www.w3.org/XML/1998/namespace}space','preserve');new.append(q)
 for op in ordered:
  a,z=op['start'],op['end'];require(cursor<=a<=z<=len(total),'overlapping_or_outside_slot');require(not any(value is None and a<x<z for x,y,n,value in atoms),'nontext_inside_replaced_placeholder');fixed(cursor,a);new.append(b.sdt(op['tag'],op['label']));cursor=z
 fixed(cursor,len(total),True)
 for child in list(p):
  if child.tag!=W+'pPr':p.remove(child)
 for n in new:p.append(n)

def compile_generic(path,cfg,res,other):
 parts,root=b.load_doc(path);repairs=normalize(root);body=b.tbody(root);slots=[];repeats=[]
 for policy in cfg['table_policies']:
  table=body[policy['body']];require(table.tag==W+'tbl','source_table_anchor');rows=table.findall(W+'tr');old=len(rows);needed=policy['header_rows']+len(policy['record_keys'])
  while len(table.findall(W+'tr'))<needed:table.append(copy.deepcopy(rows[-1]))
  repeats.append({'source_rows':old,'final_rows':len(table.findall(W+'tr')),'records':policy['record_keys'],'unused_original_rows_preserved':True})
 ops={};attachments=[]
 for r in cfg['rules']:
  loc=r['location'];node=body[loc['body']];kind=loc['kind'];p=None;span=None
  if kind in ('cell','cell_pattern','cell_attachment'):
   rows=node.findall(W+'tr');require(loc['row']<len(rows),'source_row_missing');cells=rows[loc['row']].findall(W+'tc');require(loc['column']<len(cells),'physical_cell_misaligned');cell=cells[loc['column']];ps=cell.findall(W+'p');require(bool(ps),'cell_no_paragraph');p=ps[0]
   if kind=='cell':require(blanknode(cell),'not_genuine_blank_cell');span=(0,len(text(p)))
  elif kind=='paragraph_blank':p=node;require(node.tag==W+'p' and blanknode(node),'not_genuine_blank_paragraph');span=(0,len(text(p)))
  else:p=node;require(node.tag==W+'p','paragraph_anchor')
  if kind in ('pattern','cell_pattern'):
   ms=list(re.finditer(loc['pattern'],text(p)));occ=r.get('occurrence',0);require(len(ms)>occ,'missing_exact_source_pattern');m=ms[occ];span=(m.start(),m.end()) if loc['where']=='replace_token' else (m.start(),m.start()) if loc['where']=='insert_before' else (m.end(),m.end())
  if kind=='text_span':span=(loc['start'],loc['end']);require(text(p)[span[0]:span[1]].strip()=='' or text(p)[span[0]:span[1]].startswith('（'),'not_genuine_blank_span')
  ot,oi,side=rule_owner(r,res);ss=slot(r,cfg['id'],loc,owner=oi,owner_type=ot,identity_side=side,kind='image' if r['mode']=='image' else 'text',mode=r['mode'],path=r.get('path'),transform=r['transform'],source_refs=binding(r,res),origin='generic',hard_required=r.get('mandatory',False));slots.append(ss)
  if ss['kind']=='image':attachments.append((p,ss));continue
  require(span is not None,'missing_source_span');ops.setdefault(p,[]).append(dict(ss,start=span[0],end=span[1]))
 for p,arr in ops.items():inject(p,arr)
 for p,ss in attachments:p.append(b.sdt(ss['tag'],ss['label']))
 # Compact dedicated-skill row policy expands every untouched ORIGINAL empty data cell.
 # This is not a record invented to fill a row: no assigned frozen source => label only.
 for policy in cfg['table_policies']:
  if policy.get('unused_cells_policy')!='semantic_labels_all_original_blanks':continue
  table=body[policy['body']];rows=table.findall(W+'tr');headers=[text(c).strip() for c in rows[0].findall(W+'tc')]
  for ri,row in enumerate(rows[policy['first_data_row']:],policy['first_data_row']):
   for ci,cell in enumerate(row.findall(W+'tc')):
    if cell.find('.//'+W+'sdt') is not None:continue
    is_image=ci in policy.get('attachment_columns',[])
    if not blanknode(cell) and not is_image:continue
    loc={'body':policy['body'],'row':ri,'column':ci,'kind':'original_unassigned_cell'};rr={'id':'original-unassigned.'+str(policy['body'])+'.'+str(ri)+'.'+str(ci),'label':headers[ci]+'（第'+str(ri)+'行）'};ss=slot(rr,cfg['id'],loc,kind='image' if is_image else 'text',origin='unassigned_original_row',mode='abstain',path=None,transform='string',source_refs=[],hard_required=False);cell.find(W+'p').append(b.sdt(ss['tag'],ss['label']));slots.append(ss)
 return parts,root,{'template_id':cfg['id'],'slots':slots,'repeat_maps':repeats,'normalizations':repairs,'protected':cfg['mode'] in ('protected','reference')}

def compile_resume(path,cfg,res,other):
 parts,root=b.load_doc(path);repairs=normalize(root);body=b.tbody(root);source=list(body);require(len(source)==6 if source[-1].tag==W+'sectPr' else len(source)==5,'resume_source_shape');source=[n for n in source if n.tag!=W+'sectPr'];section=copy.deepcopy(body.find(W+'sectPr'))
 for n in list(body):body.remove(n)
 slots=[];repeats=[]
 for pid in cfg['selected_people']:
  pending={};nodes=[copy.deepcopy(n) for n in source];table=next(n for n in nodes if n.tag==W+'tbl');rows=table.findall(W+'tr');require(len(rows)==11,'resume_blank_rows_truncated');rec=res['records']['person:'+pid];refs=[{'key':'person:'+pid,'id':rec['id'],'version':rec['version'],'sha256':rec['sha256']}]
  def add(rr,cc,path,label,manual=False,pattern=None,where='insert_after'):
   cell=rows[rr].findall(W+'tc')[cc];p=cell.find(W+'p');r={'id':'resume.'+str(rr)+'.'+str(cc)+'.'+path,'label':label};ss=slot(r,cfg['id'],{'row':rr,'physical_column':cc,'path':path},rec['id'],origin='resume',person_alias=pid,path=path,manual=manual,source_refs=resume_refs(pid,path,res))
   if pattern:
    ms=list(re.finditer(pattern,text(p)));require(len(ms)==1,'resume_exact_pattern');m=ms[0];a=m.end() if where=='insert_after' else m.start();op=dict(ss,start=a,end=a);pending.setdefault(p,[]).append(op)
   else:require(blanknode(cell),'resume_fixed_cell');pending.setdefault(p,[]).append(dict(ss,start=0,end=len(text(p))))
   slots.append(ss)
  for rr,cc,path,label,manual in [(0,1,'name','姓名',False),(0,3,'age','年龄',True),(0,5,'education','学历',False),(1,1,'title','职称',False),(1,3,'job','职务',True),(1,5,'role','拟在本合同任职',False),(2,1,'work_years','工作年限',True),(2,3,'role_years','从事本项目拟任职务年限',True)]:add(rr,cc,path,label,manual)
  for path,label,pat,where in [('graduation_year','毕业年份','^年','insert_before'),('school','毕业学校','毕业于','insert_after'),('major','毕业专业','学校','insert_after')]:add(3,1,path,label,path=='graduation_year',pat,where)
  for rr in range(6,11):
   for cc,(path,label) in enumerate([('dates','工作经历时间'),('name','参加类似项目'),('role','经历担任职务'),('client_contact','发包人及联系电话')]):add(rr,cc,'experience:'+str(rr-6)+':'+path,label)
  for pp,oo in pending.items():inject(pp,oo)
  for n in nodes:body.append(n)
  cats=[]
  for category,label in zip(IDS_ORDER,LABELS):
   if category=='other_cert' and pid in cfg['other_requirements']:label='；'.join(q['title'] for q in cfg['other_requirements'][pid])
   elif category=='practice_cert':label=cfg.get('practice_filters',{}).get(res['records']['assignment:'+pid]['content']['role'],{}).get('title',label)
   elif category=='performance':label=cfg['performance_evidence_by_role'].get(res['records']['assignment:'+pid]['content']['role'],{}).get('title',label)
   xs,why=eligible_attachment(res,pid,category,cfg,other);r={'id':'resume.image.'+category,'label':label};ss=slot(r,cfg['id'],{'attachment_category_order':len(cats)},rec['id'],kind='image',origin='resume',person_alias=pid,category=category,attachment_ids=[a['id'] for a in xs],source_refs=refs,hard_required=False,selection_reason=why);p=etree.Element(W+'p');rr=etree.SubElement(p,W+'r');etree.SubElement(rr,W+'t').text=label+'：';p.append(b.sdt(ss['tag'],ss['label']));body.append(p);slots.append(ss);cats.append(category)
  repeats.append({'person_alias':pid,'person_uuid':rec['id'],'original_rows':11,'attachment_order':cats,'table_then_own_attachments':True})
 if section is not None:body.append(section)
 return parts,root,{'template_id':cfg['id'],'slots':slots,'repeat_maps':repeats,'normalizations':repairs,'source_note_kept_per_person':True}

def compile_one(path,cfg,res,plan):
 if cfg['mode']=='base':
  bc=cfg['base_contract'];pids=res['selected_people'] if cfg['id']=='personnel_roster' else [];certs={p:(b.cert_candidates(bc,res,p)[0]['id'] if b.cert_candidates(bc,res,p) else None) for p in pids};parts,root,meta=b.compile_semantic(path,bc,res,pids,certs);repairs=normalize(root);slots=[]
  for s in meta['slots']:slots.append({'tag':s['tag'],'template_id':cfg['id'],'rule_id':s['rule_id'],'label':s['label'],'kind':s.get('kind','text'),'origin':'base','base_slot':s,'owner_id':s.get('owner_id'),'hard_required':s.get('kind')=='image','mandatory_declaration':True})
  return parts,root,{'template_id':cfg['id'],'slots':slots,'base_metadata':meta,'normalizations':meta.get('ooxml_order_repairs',[])+repairs,'repeat_maps':meta['repeat_regions']}
 if cfg['mode']=='resume':return compile_resume(path,cfg,res,plan['other_requirements'])
 return compile_generic(path,cfg,res,plan['other_requirements'])

def fill(path,out,metadata,cfgs,res,assets,plan):
 doc=Document(path);nodes={b.stag(n):n for n in doc.element.body.iter(W+'sdt')};slots=metadata['slots'];require(set(nodes)=={s['tag'] for s in slots},'required_slot_missing_or_extra');log=[];hashes=set()
 for s in slots:
  cfg=cfgs[s['template_id']];n=nodes[s['tag']];content=n.find(W+'sdtContent')
  if s['kind']=='image':
   aa,why=image_binding(s,res,cfg,plan['other_requirements']);require(bool(aa) or not s['hard_required'],'mandatory_identity_image_missing')
   if not aa:log.append({'tag':s['tag'],'rule_id':s['rule_id'],'action':'label','reason':why});continue
   for a in aa:verify_image(a,res,assets)
   for c in list(content):content.remove(c)
   for a in aa:
    pth=verify_image(a,res,assets);p=doc.add_paragraph();p.add_run().add_picture(str(pth),width=Inches(2.3));el=p._p;run=el.find(W+'r');el.remove(run);content.append(run);el.getparent().remove(el);hashes.add(a['sha256']);log.append({'tag':s['tag'],'rule_id':s['rule_id'],'action':'image','reason':'ok','attachment_id':a['id'],'owner_type':a['owner_type'],'owner_id':a['owner_id'],'sha256':a['sha256'],'current':True})
  else:
   value,why=resolve(s,cfg,res)
   if value is None:log.append({'tag':s['tag'],'rule_id':s['rule_id'],'action':'label','reason':why});continue
   for c in list(content):content.remove(c)
   r=etree.SubElement(content,W+'r');rp=etree.SubElement(r,W+'rPr');etree.SubElement(rp,W+'color').set(W+'val','008000');etree.SubElement(r,W+'t').text=value;log.append({'tag':s['tag'],'rule_id':s['rule_id'],'action':'text','reason':'ok','source_refs':s.get('source_refs',s.get('base_slot',{})),'value':value})
 b.save_filled_package(path,out,doc,hashes);audit=check_fill(path,out,metadata,cfgs,res,assets,plan);audit['writes']=log;return audit

def check_fill(before,after,metadata,cfgs,res,assets,plan):
 _,r1=b.load_doc(before);_,r2=b.load_doc(after);require(b.frame(r1)==b.frame(r2),'outside_slot_changed');doc=Document(after);nodes={b.stag(n):n for n in doc.element.body.iter(W+'sdt')};require(set(nodes)=={s['tag'] for s in metadata['slots']},'required_slot_missing_or_extra');hashes=[];counts={'text_filled':0,'images_filled':0,'semantic_labels':0,'mandatory_missing_values':0};gaps=[]
 for s in metadata['slots']:
  n=nodes[s['tag']];cfg=cfgs[s['template_id']]
  if s['kind']=='image':
   aa,why=image_binding(s,res,cfg,plan['other_requirements']);require(bool(aa) or not s['hard_required'],'mandatory_identity_image_missing');expected=[a['sha256'] for a in aa];actual=[hashlib.sha256(doc.part.related_parts[n.get(R+'embed')].blob).hexdigest() for n in n.iter(A+'blip')];require(actual==expected,'actual_image_owner_side_hash_or_omission');hashes+=expected;counts['images_filled']+=len(aa)
   for a in aa:verify_image(a,res,assets)
   if aa:continue
   value=None
  else:
   value,why=resolve(s,cfg,res)
   if value is not None:require(text(n)==value,'wrong_supported_value');counts['text_filled']+=1;continue
  require(text(n)=='【'+s['label']+'】','missing_label_replaced_by_prose');require(all(c.get(W+'val')=='C09000' for c in n.iter(W+'color')),'missing_not_yellow');counts['semantic_labels']+=1;counts['mandatory_missing_values']+=1;gaps.append({'template_id':s['template_id'],'tag':s['tag'],'label':s['label'],'owner_id':s.get('owner_id'),'reason':why})
 b.package_fixed(before,after,True,hashes);return {'ok':True,**counts,'outside_slot_and_package_unchanged':True,'supported_but_unfilled':0,'gaps':gaps,'formal_bid_ready':False}

class Engine:
 def __init__(self,args,root='/'):
  self.args=args;self.root=Path(root);self.assets=unpack(self.root/args['assets_bundle'].lstrip('/'),self.root/'workspace/frozen');self.scope=read(self.assets/'inputs/scope.json');self.res=read(self.assets/'inputs/resources.json');require(sha(self.assets/'inputs/original.docx')==self.scope['source_sha256'] and sha(self.assets/'inputs/resources.json')==self.scope['resources_sha256'],'source_snapshot_hash');ns=[d.name for d in (self.assets/'skills').iterdir() if d.is_dir()];require(len(ns)==1,'skill_namespace_ambiguous');self.ns=ns[0];self.cfg={}
  for x in self.scope['templates']:
   tid=x['id'];p=self.assets/'skills'/self.ns/tid/'resources/stage-contract.json';require(sha(p)==self.scope['contract_sha256'][tid],'skill_hash_version');c=read(p);require([r['id'] for r in c['rules']]==self.scope['slot_requirement_inventory'][tid]['rule_ids'],'undeclared_source_requirement');self.cfg[tid]=c
  self.previous=unpack(self.root/args['prior_bundle'].lstrip('/'),self.root/'workspace/parents') if args['stage']!='extract' else None
  if self.previous:
   self.prior=read(self.previous/'stage-manifest.json');require(self.prior['stage']==('extract' if args['stage']=='semantic' else 'semantic'),'stage_order');require(self.prior['source_sha256']==self.scope['source_sha256'] and self.prior['resources_sha256']==self.scope['resources_sha256'],'parent_snapshot_mismatch')
   for d in self.prior['documents'].values():require(sha(self.previous/Path(d['path']).name)==d['sha256'],'parent_artifact_hash')
 def load(self,ids):
  require(set(ids)<=set(self.cfg) and 0<len(ids)<=8,'skill_batch_scope');rows=[]
  for tid in ids:
   p=self.assets/'skills'/self.ns/tid;rows.append({'template_id':tid,'entry':(p/'SKILL.md').read_text(),'entry_sha256':sha(p/'SKILL.md'),'contract':self.cfg[tid],'contract_sha256':self.scope['contract_sha256'][tid]})
  return {'files':[],'loaded_dedicated_skill_packages':rows,'independent_copies':True}
 def inspect(self):
  res={'files':[],'stage':self.args['stage'],'source_sha256':self.scope['source_sha256'],'resources_sha256':self.scope['resources_sha256'],'contracts_revision':dh(self.scope['contract_sha256']),'templates':[{'id':x['id'],'range':[x['start'],x['end']],'heading':x['heading'],'mode':x['mode'],'rule_count':len(self.cfg[x['id']]['rules']),'source_tables':x['tables']} for x in self.scope['templates']],'source_all_nodes_classified_once':True,'reference_only_ranges':self.scope['reference_only_ranges'],'selected_person_ids':self.res['selected_people'],'other_certificate_requirements':[]}
  c=self.cfg.get('personnel_resume',{'other_requirements':{}})
  for pid,qs in c['other_requirements'].items():
   p=self.res['records']['person:'+pid]['id'];f=qs[0]['certificate_filter'];ref=self.res['reference']['date'];cs=[q for q in self.res['native']['material_certificates'] if q['person_id']==p and f.get('name_contains','') in q['name'] and all(q.get(k)==v for k,v in f.get('equals',{}).items()) and not q.get('deleted_at') and (not q.get('expires_on') or q['expires_on']>=ref) and (not q.get('issued_on') or q['issued_on']<=ref)];res['other_certificate_requirements'].append({'person_id':pid,'required_title':qs[0]['title'],'clause_id':qs[0]['clause'],'source_quote':qs[0]['quote'],'candidate_certificate_ids':[q['id'] for q in cs]})
  return res
 def validate_plan(self,plan):
  require(plan['source_sha256']==self.scope['source_sha256'] and plan['resources_sha256']==self.scope['resources_sha256'] and plan['contracts_revision']==dh(self.scope['contract_sha256']),'plan_source_skill_version');require(plan['template_ids']==list(self.cfg),'missing_or_reordered_template');require(plan['reason']=='ok' and plan['rule_policy']=='all_declared','plan_reason_policy');require(plan['source_ranges']=={x['id']:[x['start'],x['end']] for x in self.scope['templates']},'source_range_wrong_or_omitted')
  if self.args['stage']!='extract':
   require(plan['person_ids']==self.res['selected_people'],'wrong_selected_project_assignments');expected=self.cfg['personnel_resume']['other_requirements'] if 'personnel_resume' in self.cfg else {};require(set(plan['other_requirements'])==set(expected),'undeclared_other_certificate_requirement')
   for pid,qs in expected.items():
    q=plan['other_requirements'][pid];require(self.res['records']['assignment:'+pid]['content']['role']==qs[0]['applicable_role'],'other_requirement_role_mismatch');require((q['title'],q['clause_id'],q['quote'])==(qs[0]['title'],qs[0]['clause'],qs[0]['quote']),'other_title_without_applicable_clause');require(q['quote'] in self.res['source_clauses'][q['clause_id']],'forged_clause_quote');eligible_attachment(self.res,pid,'other_cert',self.cfg['personnel_resume'],plan['other_requirements'])
 def execute(self,call):
  plan=self.args['plan'];self.validate_plan(plan);stage=self.args['stage'];out=self.root/'output'/call;out.mkdir(parents=True,exist_ok=False);documents={};files=[];allslots=[];allnodes=[];parts0,source=b.load_doc(self.assets/'inputs/original.docx');sourcebody=list(b.tbody(source));section=copy.deepcopy(b.tbody(source).find(W+'sectPr'));audits={}
  for inv in self.scope['templates']:
   tid=inv['id'];cfg=self.cfg[tid];dest=out/(tid+'.'+stage+'.docx')
   if stage=='extract':
    parts=parts0.copy();root=copy.deepcopy(source);body=b.tbody(root)
    for n in list(body):body.remove(n)
    selected=[copy.deepcopy(n) for n in sourcebody[inv['start']:inv['end']]]
    require([hashlib.sha256(canon(n)).hexdigest() for n in sourcebody[inv['start']:inv['end']]]==inv['source_node_hashes'],'original_rows_or_prefill_changed')
    for n in selected:body.append(n)
    body.append(copy.deepcopy(section));meta={'template_id':tid,'source_range':[inv['start'],inv['end']],'source_rows':inv['tables'],'slots':[]};audit={'exact_source_xml':True,'empty_rows_merged_cells_media_preserved':True}
   elif stage=='semantic':parts,root,meta=compile_one(self.previous/Path(self.prior['documents'][tid]['path']).name,cfg,self.res,plan);audit={'normalizations':meta.get('normalizations',[]),'slots':len(meta['slots']),'predeclared_repeat_maps':meta.get('repeat_maps',[]),'no_fill_time_layout':True}
   else:
    meta=self.prior['documents'][tid]['metadata'];audit=fill(self.previous/Path(self.prior['documents'][tid]['path']).name,dest,meta,self.cfg,self.res,self.assets,plan);parts,root=b.load_doc(dest)
   if stage!='fill':
    b.save_doc(dest,parts,root);b.package_fixed(self.assets/'inputs/original.docx' if stage=='extract' else self.previous/Path(self.prior['documents'][tid]['path']).name,dest)
    if stage=='extract':
     _,actual=b.load_doc(dest);require([hashlib.sha256(canon(n)).hexdigest() for n in b.tbody(actual) if n.tag!=W+'sectPr']==inv['source_node_hashes'],'saved_source_XML_fidelity')
   if stage!='fill':allnodes.extend(copy.deepcopy(n) for n in b.tbody(root) if n.tag!=W+'sectPr')
   allslots.extend(meta['slots']);documents[tid]={'path':'/output/'+call+'/'+dest.name,'sha256':sha(dest),'metadata':meta,'audit':audit};files.append('/output/'+call+'/'+dest.name);audits[tid]=audit
  combined=out/('combined-response.'+stage+'.docx')
  if stage!='fill':
   root=copy.deepcopy(source);body=b.tbody(root)
   for n in list(body):body.remove(n)
   for n in allnodes:body.append(n)
   body.append(section);b.save_doc(combined,parts0,root);combinedaudit={'ok':True,'all_inventory_units_in_original_order':True}
  else:combinedaudit=fill(self.previous/Path(self.prior['combined']['path']).name,combined,{'slots':allslots},self.cfg,self.res,self.assets,plan)
  files.append('/output/'+call+'/'+combined.name);manifest={'schema':'full-stage/1','stage':stage,'source_sha256':self.scope['source_sha256'],'resources_sha256':self.scope['resources_sha256'],'contracts_revision':dh(self.scope['contract_sha256']),'template_ids':list(self.cfg),'documents':documents,'combined':{'path':'/output/'+call+'/'+combined.name,'sha256':sha(combined),'audit':combinedaudit},'parent_manifest_sha256':sha(self.previous/'stage-manifest.json') if self.previous else None,'plan':plan,'all_stage_gates':True,'formal_bid_ready':False};write(out/'stage-manifest.json',manifest);files.append('/output/'+call+'/stage-manifest.json');summary={'stage':stage,'templates':len(documents),'source_inventory_complete':True,'combined':{k:v for k,v in combinedaudit.items() if k not in ('gaps','writes')},'individual_counts':{t:{k:v for k,v in a.items() if k in ('text_filled','images_filled','semantic_labels','supported_but_unfilled')} for t,a in audits.items()},'formal_bid_ready':False};write(out/'summary.json',summary);files.append('/output/'+call+'/summary.json');return {'files':files,'manifest_path':'/output/'+call+'/stage-manifest.json','summary':{k:v for k,v in summary.items() if k!='combined'},'combined_counts':{k:v for k,v in combinedaudit.items() if k not in ('gaps','writes')},'all_stage_gates':True}
def bind_constants(args,operation,stage):
 require(isinstance(args,dict),'payload_object')
 for k,v in (('operation',operation),('stage',stage)):
  if v is None:require(k in args,'missing_'+k)
  else:require(args.setdefault(k,v)==v,'bound_argument_mismatch')
 return args
def cli(argv=None,root='/'):
 p=argparse.ArgumentParser();p.add_argument('payload');p.add_argument('--call',required=True);p.add_argument('--operation',choices=['load','inspect','execute']);p.add_argument('--stage',choices=['extract','semantic','fill']);a=p.parse_args(argv)
 try:
  require(bool(re.fullmatch('[a-f0-9]{1,64}',a.call)),'call_identity');args=bind_constants(json.loads(a.payload),a.operation,a.stage);e=Engine(args,root);op=args['operation'];r=e.load(args['template_ids']) if op=='load' else e.inspect() if op=='inspect' else e.execute(a.call);print(json.dumps(r,ensure_ascii=False));return 0
 except b.GateError as ex:print(json.dumps({'safe_gate':str(ex)}));return 2
 except Exception as ex:print(json.dumps({'safe_error_type':type(ex).__name__}));return 2
if __name__=='__main__':raise SystemExit(cli())
