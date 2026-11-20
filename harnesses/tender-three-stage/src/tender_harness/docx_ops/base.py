"""Constrained three-stage DOCX operations. Production writes ONLY in sandbox /output.
Source extraction is raw-package preserving, never the text-only demonstration slicer.
Business mappings, eligibility and abstention policy are read from independent Skills.
"""
from pathlib import Path
import json,hashlib,copy,re,zipfile,io,datetime,argparse,sys
from lxml import etree
from docx import Document
from docx.shared import Inches
W='{http://schemas.openxmlformats.org/wordprocessingml/2006/main}';A='{http://schemas.openxmlformats.org/drawingml/2006/main}';R='{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'
class GateError(Exception):pass
def require(test,code):
 if not test:raise GateError(code)
def dh(v):return hashlib.sha256(json.dumps(v,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def text(n):return ''.join(t.text or '' for t in n.iter(W+'t'))
def canon(n):return etree.tostring(n,method='c14n')
def load_doc(p):
 with zipfile.ZipFile(p) as z:parts={n:z.read(n) for n in z.namelist()}
 return parts,etree.fromstring(parts['word/document.xml'])
def save_doc(p,parts,root):
 with zipfile.ZipFile(p,'w',zipfile.ZIP_DEFLATED) as z:
  for n,b in parts.items():z.writestr(n,etree.tostring(root,xml_declaration=True,encoding='UTF-8',standalone=True) if n=='word/document.xml' else b)
def tbody(root):return root.find(W+'body')
def stag(n):return n.find(W+'sdtPr').find(W+'tag').get(W+'val')
def shape(n):
 return {'rows':len(n.findall(W+'tr')),'grid':[x.get(W+'w') for x in n.findall(W+'tblGrid/'+W+'gridCol')],'physical_cells':[[{'text':text(c),'tcPr_sha256':hashlib.sha256(canon(c.find(W+'tcPr'))).hexdigest() if c.find(W+'tcPr') is not None else None} for c in r.findall(W+'tc')] for r in n.findall(W+'tr')]}
def package_fixed(before_path,after_path,allow_images=False,media_hashes=()):
 b,_=load_doc(before_path);a,_=load_doc(after_path);require(set(b)<=set(a),'package_part_removed')
 for n in set(a)-set(b):require(allow_images and n.startswith('word/media/') and hashlib.sha256(a[n]).hexdigest() in media_hashes,'undeclared_package_addition')
 for n,x in b.items():
  if n=='word/document.xml':continue
  y=a[n]
  if allow_images and n=='word/_rels/document.xml.rels':
   old=etree.fromstring(x);new=etree.fromstring(y);ids={e.get('Id') for e in old}
   for e in list(new):
    if e.get('Id') not in ids:
     import posixpath
     target=posixpath.normpath('word/'+e.get('Target',''))
     require(e.get('Type','').endswith('/image') and e.get('TargetMode')!='External' and target in a and hashlib.sha256(a[target]).hexdigest() in media_hashes,'wrong_image_relationship');new.remove(e)
   require(canon(old)==canon(new),'existing_relationship_mutated')
  elif allow_images and n=='[Content_Types].xml':
   old=etree.fromstring(x);new=etree.fromstring(y);entries={(e.tag,tuple(sorted(e.attrib.items()))) for e in old}
   for e in list(new):
    if (e.tag,tuple(sorted(e.attrib.items()))) not in entries:
     require(e.get('ContentType','').startswith('image/') and any(p.startswith('word/media/') and p.endswith('.'+e.get('Extension','!')) for p in a),'nonimage_content_type');new.remove(e)
   require(canon(old)==canon(new),'content_type_mutated')
  elif n.endswith(('.xml','.rels')):require(canon(etree.fromstring(x))==canon(etree.fromstring(y)),'outside_package_xml:'+n)
  else:require(x==y,'outside_package_bytes:'+n)
def sdt(tag,label):
 n=etree.Element(W+'sdt');pr=etree.SubElement(n,W+'sdtPr');etree.SubElement(pr,W+'id').set(W+'val',str(int(hashlib.sha256(tag.encode()).hexdigest()[:7],16)+1));etree.SubElement(pr,W+'tag').set(W+'val',tag);c=etree.SubElement(n,W+'sdtContent');r=etree.SubElement(c,W+'r');rp=etree.SubElement(r,W+'rPr');etree.SubElement(rp,W+'color').set(W+'val','C09000');etree.SubElement(r,W+'t').text='【'+label+'】';return n
def inject(p,ops):
 """Only declared exact text offsets; copy every fixed run's rPr, never rewrite prose."""
 total=text(p);last=0
 for o in sorted(ops,key=lambda x:x['start']):require(o['start']>=last and 0<=o['start']<=o['end']<=len(total),'overlapping_slot');last=o['end']
 runs=[c for c in p if c.tag==W+'r'];require(all(c.tag in (W+'r',W+'pPr') for c in p),'unsupported_paragraph_structure')
 require(all(all(c.tag in (W+'rPr',W+'t') for c in r) for r in runs),'nontext_run_not_slot_safe')
 spans=[];pos=0
 for r in runs:spans.append((pos,pos+len(text(r)),r));pos+=len(text(r))
 def fixed(a,b):
  out=[]
  for x,y,r in spans:
   lo=max(a,x);hi=min(b,y)
   if lo<hi:
    q=copy.deepcopy(r)
    for c in list(q):
     if c.tag!=W+'rPr':q.remove(c)
    t=etree.SubElement(q,W+'t');t.set('{http://www.w3.org/XML/1998/namespace}space','preserve');t.text=total[lo:hi];out.append(q)
  return out
 new=[];cursor=0
 for o in sorted(ops,key=lambda x:x['start']):new.extend(fixed(cursor,o['start']));new.append(sdt(o['tag'],o['label']));cursor=o['end']
 new.extend(fixed(cursor,len(total)))
 for c in list(p):
  if c.tag!=W+'pPr':p.remove(c)
 for n in new:p.append(n)
def get_record(resources,kind,person=None,cert=None):
 if kind=='person_certificate':
  if cert is None:return None
  parent=resources['records'].get('person:'+str(person));require(parent is not None,'outside_selected_person')
  c=next((x for x in parent['content'].get('certificates',[]) if x.get('id')==cert),None);require(c is not None and c.get('person_id')==parent['id'] and not c.get('deleted_at'),'wrong_certificate_owner')
  return {'id':cert,'kind':kind,'version':parent['version'],'owner_id':person,'content':c,'sha256':dh(c)}
 ident=person if kind in ('person','assignment') else resources['legal_owner_id'] if kind=='legal_representative' else kind
 rec=resources['records'].get(kind+':'+str(ident));require(rec is not None,'missing_source_record:'+kind);require(rec['sha256']==dh(rec['content']) and rec['version']==1,'source_version_or_hash');return rec

def eligible(cfg,res):return [a['person_id'] for a in res['assignments'] if a['role'] not in cfg.get('exclude_roles',[])]
def cert_candidates(cfg,res,pid):
 role=res['records']['assignment:'+pid]['content']['role'];rule=cfg['certificate_preferences'][role];cs=res['records']['person:'+pid]['content'].get('certificates',[])
 good=[c for c in cs if not c.get('deleted_at') and rule.get('name_contains','') in c.get('name','') and (not rule.get('major_equals') or c.get('major')==rule['major_equals']) and (not rule.get('level_equals') or c.get('level')==rule['level_equals'])]
 return good

def binding(rule,res,pid=None,cert=None):
 rec=get_record(res,rule['source_kind'],pid,cert)
 return {'source_kind':rule['source_kind'],'source_id':rec['id'] if rec else None,'source_version':rec['version'] if rec else None,'source_sha256':rec['sha256'] if rec else None,'owner_id':pid or (res['legal_owner_id'] if rule['source_kind']=='legal_representative' else None),'path':rule['path'],'value_type':rule['value_type'],'mode':rule['mode']}
def resolve(slot,cfg,res):
 rule=cfg.get('fill_rules',{}).get(slot['rule_id'])
 if not rule:return None,'rule_missing'
 for k in ['source_kind','path','value_type','mode']:require(rule[k]==slot[k],'field_mapping_conflict')
 if slot['mode']=='abstain':return None,'bidder_decision_or_narrative'
 rec=get_record(res,slot['source_kind'],slot.get('owner_id'),slot.get('source_id') if slot['source_kind']=='person_certificate' else None)
 if rec is None:return None,'selected_certificate_major_conflict'
 require((rec['id'],rec['version'],rec['sha256'])==(slot['source_id'],slot['source_version'],slot['source_sha256']),'source_id_version_hash_mismatch')
 value=rec['content']
 for k in slot['path'].split('.'):value=value.get(k) if isinstance(value,dict) else None
 if slot['value_type']=='derived_age':
  require(cfg['reference_date'].startswith('original tender B5'),'reference_rule_missing')
  if not value:return None,'source_birth_date_missing'
  born=datetime.date.fromisoformat(value);ref=datetime.date.fromisoformat(res['reference']['date']);value=ref.year-born.year-((ref.month,ref.day)<(born.month,born.day))
 if value is None or value=='':return None,'source_value_missing'
 require(isinstance(value,(str,int,float)) and not isinstance(value,bool),'source_type_mismatch')
 return str(value),'ok'


def normalize_ppr_order(root):
 # ECMA-376 CT_PPr sequence. Source legal proof wrongly puts divId before pBdr.
 # Preserve EVERY original property node/value; only schema ordering changes.
 order='pStyle keepNext keepLines pageBreakBefore framePr widowControl numPr suppressLineNumbers pBdr shd tabs suppressAutoHyphens kinsoku wordWrap overflowPunct topLinePunct autoSpaceDE autoSpaceDN bidi adjustRightInd snapToGrid spacing ind contextualSpacing mirrorIndents suppressOverlap jc textDirection textAlignment textboxTightWrap outlineLvl divId cnfStyle rPr sectPr pPrChange'.split();rank={W+n:i for i,n in enumerate(order)};repairs=[]
 for i,p in enumerate(root.iter(W+'pPr')):
  children=list(p);require(all(n.tag in rank for n in children),'unknown_paragraph_property_order');ordered=sorted(children,key=lambda n:rank[n.tag])
  if children==ordered:continue
  before=sorted(canon(n) for n in children)
  for n in children:p.remove(n)
  for n in ordered:p.append(n)
  require(before==sorted(canon(n) for n in p),'paragraph_property_value_changed');repairs.append({'paragraph_property_index':i,'before':[n.tag.split('}')[-1] for n in children],'after':[n.tag.split('}')[-1] for n in ordered],'property_nodes_sha256':dh([x.hex() for x in before]),'property_values_unchanged':True,'reason':'original divId order violates CT_PPr schema; not style/value rewriting'})
 return repairs

def compile_semantic(extracted,cfg,res,person_ids,cert_map):
 parts,root=load_doc(extracted);repairs=normalize_ppr_order(root);body=tbody(root);slots=[];fixed=[];regions=[]
 def spec(rule,locator,pid=None,cert=None):
  tag='t113:'+hashlib.sha256((cfg['id']+'|'+rule['id']+'|'+str(pid or '')+'|'+json.dumps(locator,sort_keys=True)).encode()).hexdigest()[:24]
  out={'tag':tag,'rule_id':rule['id'],'label':(res['records']['assignment:'+pid]['content']['role']+'·' if pid else '')+rule['label'],'mandatory':True,'locator':locator,**binding(rule,res,pid,cert)};slots.append(out);return out
 operations={}
 for rule in cfg.get('fields',[]):
  matches=[]
  for i,p in enumerate(body):
   if p.tag==W+'p':
    for m in re.finditer(rule['pattern'],text(p)):matches.append((i,m))
  require(len(matches)==1,'ambiguous_or_missing_genuine_anchor:'+rule['id']);i,m=matches[0]
  start,end=(m.start(),m.end()) if rule['location']=='replace_token' else (m.start(),m.start()) if rule['location']=='insert_before' else (m.end(),m.end())
  slot=spec(rule,{'body_index':i,'start':start,'end':end,'source_fragment':text(body[i])[start:end]});operations.setdefault(i,[]).append(dict(slot,start=start,end=end))
 for i,ops in operations.items():inject(body[i],ops)
 if cfg.get('images') is not None:
  matches=[i for i,n in enumerate(body) if n.tag==W+'p' and text(n)==cfg['image_anchor']];require(len(matches)==1,'mandatory_attachment_anchor_missing');idx=matches[0]-1;require(idx>=0 and body[idx].tag==W+'p' and not text(body[idx]).strip(),'no_exact_attachment_blank')
  for j,im in enumerate(cfg['images']):
   a=res['images'][im['fixture']];tag='t113:'+hashlib.sha256((cfg['id']+'|'+im['id']).encode()).hexdigest()[:24];d={'tag':tag,'rule_id':im['id'],'label':im['label'],'kind':'image','mandatory':True,'locator':{'body_index':idx,'attachment_anchor':cfg['image_anchor'],'order':j},'attachment_id':a['id'],'owner_type':a['owner_type'],'owner_id':a['owner_id'],'category':a['category'],'side':im['side'],'fixture':im['fixture'],'sha256':a['sha256'],'source_kind':'legal_attachment'};slots.append(d)
   if j:r=etree.SubElement(body[idx],W+'r');etree.SubElement(r,W+'br')
   body[idx].append(sdt(tag,d['label']))
  require(len(cfg['images'])==2,'undeclared_source_identity_requirement')
 if cfg.get('columns'):
  require(person_ids==eligible(cfg,res),'selected_project_roles_mismatch');require(set(cert_map)==set(person_ids),'certificate_plan_incomplete')
  tables=[n for n in body if n.tag==W+'tbl'];require(len(tables)==1,'ambiguous_roster_table');table=tables[0];rows=table.findall(W+'tr');old_count=len(rows);header=cfg['header_rows'];require(old_count>header,'original_data_rows_missing');require([text(c).strip() for c in rows[0].findall(W+'tc')]==cfg['table_header'],'merged_header_mismatch')
  require(all(len(row.findall(W+'tc'))==len(cfg['columns']) for row in rows[header:]),'data_grid_misaligned')
  prototype=rows[-1];require(not text(prototype).strip(),'repeat_prototype_not_blank')
  while len(table.findall(W+'tr'))<header+len(person_ids):table.append(copy.deepcopy(prototype))
  regions.append({'kind':'explicit_person_repeat','source_rows':old_count,'header_rows':header,'retained_source_data_rows':old_count-header,'final_rows':len(table.findall(W+'tr')),'added_rows':max(0,header+len(person_ids)-old_count),'person_ids':person_ids,'certificate_ids':cert_map,'no_resumes_or_attachment_regions':True})
  for i,pid in enumerate(person_ids):
   allowed=cert_candidates(cfg,res,pid);chosen=cert_map[pid];require((chosen is None and not allowed) or any(c['id']==chosen for c in allowed),'role_clause_certificate_mismatch')
   row=table.findall(W+'tr')[header+i]
   for rule in cfg['columns']:
    cell=row.findall(W+'tc')[rule['column']];before=text(cell)
    if before.strip():
     expected=res['records']['assignment:'+pid]['content']['role'];require(rule['id']=='person.role' and before.strip()==expected,'fixed_cell_not_editable');fixed.append({'rule_id':rule['id'],'owner_id':pid,'fixed_text':before,'source':'assignment:'+pid});continue
    slot=spec(rule,{'row':header+i,'physical_column':rule['column']},pid,chosen)
    paragraphs=cell.findall(W+'p');require(len(paragraphs)==1,'unsupported_blank_cell_shape');p=paragraphs[0]
    for child in list(p):
     if child.tag!=W+'pPr':p.remove(child)
    p.append(sdt(slot['tag'],slot['label']))
 return parts,root,{'template_id':cfg['id'],'ooxml_order_repairs':repairs,'slots':slots,'fixed_bindings':fixed,'repeat_regions':regions,'person_ids':person_ids,'certificate_ids':cert_map,'supported_fields_compile_from_actual_source':True,'expected_slot_count':len(slots)}

def frame(root):
 root=copy.deepcopy(root)
 for s in root.iter(W+'sdt'):
  c=s.find(W+'sdtContent')
  for x in list(c):c.remove(x)
 return canon(root)
def image_source(slot,cfg,res,paths):
 rule=cfg.get('fill_rules',{}).get(slot['rule_id']);require(rule is not None,'required_image_rule_missing');require(rule['fixture']==slot['fixture'] and rule['side']==slot['side'] and rule['source_kind']=='legal_attachment','image_rule_mismatch')
 a=res['images'][slot['fixture']];require((a['id'],a['owner_type'],a['owner_id'],a['category'],a['poc_identity_side'],a['sha256'])==(slot['attachment_id'],slot['owner_type'],slot['owner_id'],slot['category'],slot['side'],slot['sha256']),'wrong_identity_owner_side')
 require(a['is_current'] and not a.get('deleted_at') and not a.get('superseded_by_id'),'image_not_current');require(a['owner_id']==res['legal_owner_id'],'agent_legal_confusion');scope=res['owner_scope'].get(a['owner_type']+':'+a['owner_id']);require(scope and scope['table']=='material_legal_representatives','legal_owner_table')
 legal=res['records']['legal_representative:'+res['legal_owner_id']]['content'];require('person_id' not in legal,'illegal_legal_person_relation');p=paths[slot['fixture']];require(sha(p)==a['sha256'],'image_file_hash');return p


def save_filled_package(hollow,out,doc,allowed_hashes):
 """Commit only permitted XML/media deltas, never python-docx's unrelated package rewrites."""
 buf=io.BytesIO();doc.save(buf)
 with zipfile.ZipFile(hollow) as z:original={n:z.read(n) for n in z.namelist()}
 with zipfile.ZipFile(buf) as z:generated={n:z.read(n) for n in z.namelist()}
 final=dict(original);final['word/document.xml']=generated['word/document.xml']
 for name,data in generated.items():
  if name.startswith('word/media/') and name not in original:
   require(hashlib.sha256(data).hexdigest() in allowed_hashes,'undeclared_generated_image');final[name]=data
 old=etree.fromstring(original['word/_rels/document.xml.rels']);new=etree.fromstring(generated['word/_rels/document.xml.rels']);ids={n.get('Id') for n in old}
 for node in new:
  if node.get('Id') not in ids:
   import posixpath
   target=posixpath.normpath('word/'+node.get('Target',''))
   require(node.get('Type','').endswith('/image') and node.get('TargetMode')!='External' and target in final and hashlib.sha256(final[target]).hexdigest() in allowed_hashes,'undeclared_generated_relationship');old.append(copy.deepcopy(node))
 final['word/_rels/document.xml.rels']=etree.tostring(old,xml_declaration=True,encoding='UTF-8',standalone=True)
 old=etree.fromstring(original['[Content_Types].xml']);new=etree.fromstring(generated['[Content_Types].xml']);keys={(n.tag,tuple(sorted(n.attrib.items()))) for n in old}
 for node in new:
  if (node.tag,tuple(sorted(node.attrib.items()))) not in keys:
   require(node.get('ContentType','').startswith('image/'),'undeclared_generated_content_type');old.append(copy.deepcopy(node))
 final['[Content_Types].xml']=etree.tostring(old,xml_declaration=True,encoding='UTF-8',standalone=True)
 with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as z:
  for name,data in final.items():z.writestr(name,data)

def fill_one(hollow,out,meta,cfg,res,paths,active_rules):
 require(set(active_rules)=={d['id'] for d in cfg.get('fields',[])+cfg.get('columns',[])}|{d['id'] for d in cfg.get('images',[])},'required_rule_plan_omission')
 doc=Document(hollow);nodes={stag(n):n for n in doc.element.body.iter(W+'sdt')};require(set(nodes)=={s['tag'] for s in meta['slots']},'required_slot_missing_or_extra');log=[]
 for s in meta['slots']:
  node=nodes[s['tag']];content=node.find(W+'sdtContent')
  if s.get('kind')=='image':
   p=image_source(s,cfg,res,paths)
   for c in list(content):content.remove(c)
   para=doc.add_paragraph();para._p.getparent().remove(para._p);run=para.add_run();run.add_picture(str(p),width=Inches(5.5))
   for child in list(para._p):content.append(child)
   log.append({'tag':s['tag'],'reason':'ok','source_id':s['attachment_id'],'source_type':'image','owner_id':s['owner_id'],'sha256':s['sha256'],'action':'filled'});continue
  value,why=resolve(s,cfg,res)
  if value is None:log.append({'tag':s['tag'],'reason':why,'source_id':s['source_id'],'action':'retained_semantic_label'});continue
  for c in list(content):content.remove(c)
  r=etree.SubElement(content,W+'r');rp=etree.SubElement(r,W+'rPr');etree.SubElement(rp,W+'color').set(W+'val','008000');etree.SubElement(r,W+'t').text=value
  log.append({'tag':s['tag'],'reason':'ok','source_id':s['source_id'],'source_version':s['source_version'],'source_sha256':s['source_sha256'],'source_type':s['value_type'],'owner_id':s.get('owner_id'),'path':s['path'],'actual':value,'derivation':res['reference'] if s['value_type']=='derived_age' else None,'action':'filled'})
 save_filled_package(hollow,out,doc,{s['sha256'] for s in meta['slots'] if s.get('kind')=='image'});return check_fill(hollow,out,meta,cfg,res,paths)|{'before_write_bindings':log}
def check_fill(hollow,filled,meta,cfg,res,paths):
 _,before=load_doc(hollow);_,after=load_doc(filled);require(frame(before)==frame(after),'outside_slot_changed');hashes=[s['sha256'] for s in meta['slots'] if s.get('kind')=='image'];package_fixed(hollow,filled,True,hashes)
 doc=Document(filled);nodes={stag(n):n for n in doc.element.body.iter(W+'sdt')};require(set(nodes)=={s['tag'] for s in meta['slots']},'required_slot_missing_or_extra');supported=filled_count=remaining=images=0;gaps=[]
 for s in meta['slots']:
  n=nodes[s['tag']]
  if s.get('kind')=='image':
   image_source(s,cfg,res,paths);blips=list(n.iter(A+'blip'));require(len(blips)==1,'required_identity_image_missing');p=doc.part.related_parts[blips[0].get(R+'embed')];require(hashlib.sha256(p.blob).hexdigest()==s['sha256'],'wrong_actual_image_or_side');images+=1;continue
  v,why=resolve(s,cfg,res)
  if v is None:
   require(text(n)=='【'+s['label']+'】','missing_label_replaced_by_prose');require(all(c.get(W+'val')=='C09000' for c in n.iter(W+'color')),'missing_label_not_yellow');remaining+=1;gaps.append({'rule_id':s['rule_id'],'owner_id':s.get('owner_id'),'label':s['label'],'reason':why})
  else:supported+=1;require(text(n)==v,'supported_value_wrong_or_unfilled');filled_count+=1
 return {'ok':True,'template_id':cfg['id'],'declared_text_slots':len(meta['slots'])-images,'supported_text':supported,'filled_text':filled_count,'remaining_semantic_labels':remaining,'identity_required':2 if cfg.get('images') is not None else 0,'identity_filled':images,'outside_slot_and_package_unchanged':True,'unexplained_supported_unfilled':0,'gaps':gaps,'formal_bid_ready':False}

class Engine:
 def __init__(self,args,root='/'):
  self.a=args;self.root=Path(root)
  def p(k):
   value=args[k];require(isinstance(value,str) and value.startswith(('/documents/','/materials/','/skills/')) and '..' not in Path(value).parts,'path_scope');path=self.root/value.lstrip('/');require(path.is_file() and not path.is_symlink(),'missing_input:'+k);return path
  self.paths={k:p(k) for k in ['source','scope','resources','prior_manifest','legal_doc','bid_doc','roster_doc','legal_contract','bid_contract','roster_contract','legal_front','legal_back']};self.scope=json.loads(self.paths['scope'].read_text());self.res=json.loads(self.paths['resources'].read_text());require(sha(self.paths['source'])==self.scope['source_sha256'],'source_hash_version');require(sha(self.paths['resources'])==self.scope['resources_sha256'],'resource_snapshot_hash');self.cfg={}
  for tid,k in [('legal_rep_certificate','legal_contract'),('bid_letter','bid_contract'),('personnel_roster','roster_contract')]:
   require(sha(self.paths[k])==self.scope['contract_sha256'][tid],'skill_hash_version');c=json.loads(self.paths[k].read_text());require(c['id']==tid and c['schema']=='three-stage-skill/1','skill_schema');require(set(f['id'] for f in c.get('fields',[])+c.get('columns',[])+c.get('images',[]))==set(self.scope['source_requirement_inventory'][tid]['rule_ids']),'undeclared_source_requirement');self.cfg[tid]=c
  self.docs={t:self.paths[k] for t,k in [('legal_rep_certificate','legal_doc'),('bid_letter','bid_doc'),('personnel_roster','roster_doc')]};self.prior=json.loads(self.paths['prior_manifest'].read_text())
  if args['stage']!='extract':
   require(self.prior['stage']==('extract' if args['stage']=='semantic' else 'semantic'),'stage_order');require(self.prior['source_sha256']==self.scope['source_sha256'] and self.prior['resources_sha256']==self.scope['resources_sha256'],'prior_provenance_mismatch')
   for t,p in self.docs.items():require(sha(p)==self.prior['documents'][t]['sha256'],'prior_artifact_hash')
 def candidates(self,tid):
  _,root=load_doc(self.paths['source']);bs=list(tbody(root));cfg=self.cfg[tid];out=[]
  for i,n in enumerate(bs):
   if n.tag==W+'p' and re.fullmatch(cfg['start_regex'],text(n)):
    ends=[j for j in range(i+1,len(bs)) if bs[j].tag==W+'p' and re.fullmatch(cfg['end_regex'],text(bs[j]))]
    if not ends:continue
    end=ends[0]
    if cfg.get('required_contains') and not any(cfg['required_contains'] in text(x) for x in bs[i:end]):continue
    out.append({'template_id':tid,'start':i,'end':end,'start_heading':text(n),'end_heading':text(bs[end]),'block_count':end-i,'tables':[shape(x) for x in bs[i:end] if x.tag==W+'tbl']})
  return out
 def inspect(self):
  out={'stage':self.a['stage'],'source_sha256':self.scope['source_sha256'],'resources_sha256':self.scope['resources_sha256'],'templates':{}}
  for tid,cfg in self.cfg.items():
   item={'title':cfg['title'],'rule_ids':[f['id'] for f in cfg.get('fields',[])+cfg.get('columns',[])+cfg.get('images',[])]}
   if self.a['stage']=='extract':item['exact_source_candidates']=self.candidates(tid)
   elif self.a['stage']=='semantic' and cfg.get('columns'):item['selected_project_person_ids']=eligible(cfg,self.res);item['certificate_candidates']={pid:[{'id':c['id'],'name':c['name'],'major':c.get('major'),'level':c.get('level')} for c in cert_candidates(cfg,self.res,pid)] for pid in eligible(cfg,self.res)}
   elif self.a['stage']=='fill':
    meta=self.prior['documents'][tid]['metadata'];item['frozen_person_ids']=meta.get('person_ids',[]);item['frozen_certificate_ids']=meta.get('certificate_ids',{});item['slots']=[{'tag':s['tag'],'rule_id':s['rule_id'],'owner_id':s.get('owner_id'),'source_id':s.get('source_id',s.get('attachment_id')),'supported':True if s.get('kind')=='image' else resolve(s,cfg,self.res)[0] is not None,'reason':'image' if s.get('kind')=='image' else resolve(s,cfg,self.res)[1]} for s in meta['slots']]
   out['templates'][tid]=item
  return {'files':[],'inspection':out}
 def execute(self,call):
  stage=self.a['stage'];plan=self.a['plan'];require(plan['source_sha256']==self.scope['source_sha256'] and plan['resources_sha256']==self.scope['resources_sha256'],'plan_source_version');require(plan['reason']=='ok','reason_not_ok');specs={s['template_id']:s for s in plan['templates']};require(set(specs)==set(self.cfg) and len(plan['templates'])==3,'scope_or_template_omission');out=self.root/'output'/call;out.mkdir(parents=True,exist_ok=False);docs={};files=[];audits={}
  for tid,cfg in self.cfg.items():
   spec=specs[tid];dest=out/(tid+'.'+('semantic-hollow' if stage=='semantic' else 'extracted' if stage=='extract' else 'filled')+'.docx');meta={}
   if stage=='extract':
    candidates=self.candidates(tid);matches=[c for c in candidates if (c['start'],c['end'])==(spec.get('start'),spec.get('end'))];require(len(matches)==1,'wrong_source_structural_range');c=matches[0];parts,root=load_doc(self.paths['source']);body=tbody(root);nodes=list(body);selected=[copy.deepcopy(n) for n in nodes[c['start']:c['end']]];sect=copy.deepcopy(body.find(W+'sectPr'))
    for n in list(body):body.remove(n)
    for n in selected:body.append(n)
    if sect is not None:body.append(sect)
    save_doc(dest,parts,root);package_fixed(self.paths['source'],dest);_,actual=load_doc(dest);require([canon(n) for n in tbody(actual) if n.tag!=W+'sectPr']==[canon(n) for n in selected],'source_empty_rows_or_structure_lost')
    meta={'range':[c['start'],c['end']],'block_sha256':[hashlib.sha256(canon(n)).hexdigest() for n in selected],'tables':c['tables'],'source_attachment_requirements':2 if cfg.get('images') is not None else 0,'source_field_rule_ids':[f['id'] for f in cfg.get('fields',[])+cfg.get('columns',[])+cfg.get('images',[])],'source_heading':c['start_heading']};audit={'ok':True,'exact_xml_node_fidelity':True,'empty_rows_and_merged_cells_retained':True,'all_non_main_parts_byte_equal':True}
   elif stage=='semantic':
    expected={d['id'] for d in cfg.get('fields',[])+cfg.get('columns',[])+cfg.get('images',[])};require(set(spec['rule_ids'])==expected and expected==set(self.prior['documents'][tid]['metadata']['source_field_rule_ids']),'required_semantic_rule_omission');parts,root,meta=compile_semantic(self.docs[tid],cfg,self.res,spec.get('person_ids',[]),spec.get('certificate_ids',{}));save_doc(dest,parts,root);package_fixed(self.docs[tid],dest);require(sum(s.get('kind')=='image' for s in meta['slots'])==self.prior['documents'][tid]['metadata']['source_attachment_requirements'],'source_required_image_undeclared');audit={'ok':True,'genuine_anchors_only':True,'fixed_text_preserved_except_declared_source_placeholder_tokens':True,'explicit_repeat_regions':meta['repeat_regions'],'declared_slots':len(meta['slots'])}
   else:
    meta=self.prior['documents'][tid]['metadata'];require(not spec.get('person_ids') or spec['person_ids']==meta['person_ids'],'frozen_person_ids_changed');audit=fill_one(self.docs[tid],dest,meta,cfg,self.res,self.paths,spec['rule_ids'])
   mpath=out/(tid+'.'+stage+'.manifest.json');mpath.write_text(json.dumps(meta,ensure_ascii=False,indent=2));apath=out/(tid+'.'+stage+'.audit.json');apath.write_text(json.dumps(audit,ensure_ascii=False,indent=2));docs[tid]={'path':'/output/'+call+'/'+dest.name,'sha256':sha(dest),'metadata':meta,'audit':audit};files.extend(['/output/'+call+'/'+p.name for p in [dest,mpath,apath]]);audits[tid]=audit
  manifest={'schema':'t113-stage-output/1','stage':stage,'source_sha256':self.scope['source_sha256'],'resources_sha256':self.scope['resources_sha256'],'contract_sha256':self.scope['contract_sha256'],'prior_manifest_sha256':sha(self.paths['prior_manifest']),'documents':docs,'all_gates_passed':True,'formal_bid_ready':False};mp=out/'stage-manifest.json';mp.write_text(json.dumps(manifest,ensure_ascii=False,indent=2));files.append('/output/'+call+'/'+mp.name)
  return {'files':files,'stage':stage,'all_gates_passed':True,'manifest_path':'/output/'+call+'/stage-manifest.json','sha256':{p:sha(self.root/p.lstrip('/')) for p in files},'audits':audits,'formal_bid_ready':False}
if __name__=='__main__':
 parser=argparse.ArgumentParser();parser.add_argument('payload');parser.add_argument('--call',required=True);a=parser.parse_args()
 try:
  require(bool(re.fullmatch('[a-f0-9]{1,64}',a.call)),'call_identity');args=json.loads(a.payload);e=Engine(args);print(json.dumps(e.inspect() if args['operation']=='inspect' else e.execute(a.call),ensure_ascii=False))
 except GateError as e:print(json.dumps({'error':str(e)}));raise SystemExit(2)
 except Exception as e:print(json.dumps({'error_type':type(e).__name__}));raise SystemExit(2)
