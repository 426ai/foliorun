"""OFFLINE negative/structural tests on the external project pack; no model transport calls."""
import copy,asyncio
from pathlib import Path
import pytest
from tender_harness.docx_ops import engine as e
from tender_harness.offline import plan_from_inspection as plan
from tender_harness.docx_ops.requirements import source_obligations
from tender_harness.workflow import TenderThreeStage as Workflow
from tender_harness.contracts import Command,Slot
from conftest import offline_fixture
PACK,P=offline_fixture();ROOT=PACK.root;NS=PACK.namespace;S=PACK.scope;R=PACK.resources;M=e.read(P/'output/bb/stage-manifest.json');C=PACK.contracts()
@pytest.fixture
def eng():return e.Engine({'assets_bundle':'/materials/assets.zip','prior_bundle':'/materials/extract-parent.zip','stage':'semantic'},P)
def sem(t):return P/'output/bb'/(t+'.semantic.docx')
def fin(t):return P/'output/cc'/(t+'.fill.docx')
def test_full_independent_partition():
 a=[0]*2699
 for v in S['templates']+S['reference_only_ranges']:
  for i in range(v['start'],v['end']):a[i]+=1
 assert len(S['templates'])==28 and set(a)=={1}
def test_source_blank_and_upload_denominator():
 d=source_obligations(ROOT/'inputs/original.docx',{x['id']:sem(x['id']) for x in S['templates']},S['templates']);assert (d['original_blank_cells'],d['declared_original_blank_cells'],d['protected_blank_cells_intentionally_unfilled'],d['merged_continuations_not_fields'])==(927,889,30,8);assert d['original_upload_positions']==d['declared_upload_positions']==23
@pytest.mark.parametrize('field',['source_sha256','resources_sha256','contracts_revision'])
def test_wrong_snapshot(eng,field):
 p=plan(eng);p[field]='0'*64
 with pytest.raises(e.b.GateError):eng.validate_plan(p)
@pytest.mark.parametrize('change',['omit','reorder','range','person','title','cert_owner'])
def test_wrong_plans(eng,change):
 p=plan(eng)
 if change=='omit':p['template_ids'].pop()
 if change=='reorder':p['template_ids'].reverse()
 if change=='range':p['source_ranges']['authorization'][0]+=1
 if change=='person':p['person_ids'].append('P009')
 if change=='title':p['other_requirements']['P001']['title']='universal other'
 if change=='cert_owner':p['other_requirements']['P001']['selected_certificate_ids']=p['other_requirements']['P003']['selected_certificate_ids']
 with pytest.raises(e.b.GateError):eng.validate_plan(p)
@pytest.mark.parametrize('field',['id','version','sha256'])
def test_text_wrong_source(eng,field):
 s=copy.deepcopy(next(x for x in M['documents']['bidder_profile']['metadata']['slots'] if x['source_refs']));s['source_refs'][0][field]=2 if field=='version' else 'bad'
 with pytest.raises(e.b.GateError):e.resolve(s,C['bidder_profile'],R)
@pytest.mark.parametrize('field',['path','owner_id','owner_type'])
def test_text_wrong_field_or_owner(field):
 s=copy.deepcopy(next(x for x in M['documents']['bidder_profile']['metadata']['slots'] if x['source_refs']));s[field]='wrong'
 with pytest.raises(e.b.GateError):e.resolve(s,C['bidder_profile'],R)
@pytest.mark.parametrize('field',['owner_id','owner_type','identity_side'])
def test_identity_wrong_declared_owner_side(field):
 s=copy.deepcopy(next(x for x in M['documents']['authorization']['metadata']['slots'] if x['kind']=='image'));s[field]='wrong'
 with pytest.raises(e.b.GateError):e.image_binding(s,R,C['authorization'],M['plan']['other_requirements'])
@pytest.mark.parametrize('field',['is_current','sha256','owner_id'])
def test_image_current_hash_owner(field):
 a=copy.deepcopy(next(iter(R['attachments'].values())));a[field]=False if field=='is_current' else 'bad'
 with pytest.raises(e.b.GateError):e.verify_image(a,R,ROOT)
def test_missing_rule_ablation_leaves_label(tmp_path):
 cs=copy.deepcopy(C);s=next(x for x in M['documents']['bidder_profile']['metadata']['slots'] if x['source_refs']);cs['bidder_profile']['rules']=[x for x in cs['bidder_profile']['rules'] if x['id']!=s['rule_id']];out=tmp_path/'ablation.docx';a=e.fill(sem('bidder_profile'),out,M['documents']['bidder_profile']['metadata'],cs,R,ROOT,M['plan']);_,r=e.b.load_doc(out);n=next(x for x in r.iter(e.W+'sdt') if e.b.stag(x)==s['tag']);assert e.text(n)=='【'+s['label']+'】' and a['semantic_labels']>0
@pytest.mark.parametrize('change',['outside','drop_slot','drop_image','wrong_value'])
def test_actual_doc_tamper_rejected(tmp_path,change):
 tid='legal_rep_certificate';parts,r=e.b.load_doc(fin(tid));n=next(r.iter(e.W+'sdt'))
 if change=='outside':next(r.iter(e.W+'t')).text='BAD FIXED TITLE'
 elif change=='drop_slot':n.getparent().remove(n)
 elif change=='drop_image':
  n=next(x for x in r.iter(e.W+'sdt') if any(True for _ in x.iter(e.A+'blip')));content=n.find(e.W+'sdtContent');[content.remove(x) for x in list(content)]
 else:next(n.iter(e.W+'t')).text='BAD VALUE'
 p=tmp_path/'tampered.docx';e.b.save_doc(p,parts,r)
 with pytest.raises(e.b.GateError):e.check_fill(sem(tid),p,M['documents'][tid]['metadata'],C,R,ROOT,M['plan'])
def test_independent_undeclared_cell_fails(tmp_path):
 parts,r=e.b.load_doc(sem('construction_equipment'));n=next(x for x in r.iter(e.W+'sdt'));n.getparent().remove(n);q=tmp_path/'bad-sem.docx';e.b.save_doc(q,parts,r);files={x['id']:sem(x['id']) for x in S['templates']};files['construction_equipment']=q
 with pytest.raises(e.b.GateError,match='independent_original_requirement_undeclared'):source_obligations(ROOT/'inputs/original.docx',files,S['templates'])
def test_resume_rows_and_eight_order():
 m=M['documents']['personnel_resume']['metadata'];assert len(m['repeat_maps'])==8 and all(x['original_rows']==11 and x['attachment_order']==e.IDS_ORDER for x in m['repeat_maps']);_,r=e.b.load_doc(sem('personnel_resume'));assert [len(t.findall(e.W+'tr')) for t in e.b.tbody(r) if t.tag==e.W+'tbl']==[11]*8;assert R['selected_people']==['P00'+str(i) for i in range(1,9)]
def test_evidence_person_and_role_constraints():
 c=C['personnel_resume'];o=M['plan']['other_requirements'];assert not e.eligible_attachment(R,'P003','practice_cert',c,o)[0];assert not e.eligible_attachment(R,'P002','performance',c,o)[0];assert all(not e.eligible_attachment(R,p,'social_security',c,o)[0] for p in R['selected_people']);assert all(q['owner_id']==R['records']['person:P001']['id'] for q in e.eligible_attachment(R,'P001','id_card',c,o)[0])
def test_financial_explicit_currency_and_ratio():
 slots=M['documents']['financial_form']['metadata']['slots'];money=next(x for x in slots if x['transform']=='money_wan');ratio=next(x for x in slots if x['path']=='indicators.流动比率');a,_=e.resolve(money,C['financial_form'],R);b,_=e.resolve(ratio,C['financial_form'],R);assert a is not None and b is not None;assert money['locator']['kind']=='cell' and ratio['transform']=='string'
def test_semantics_all_typed_and_skill_hashes():
 for tid,d in M['documents'].items():
  assert e.sha(ROOT/'skills'/NS/tid/'resources/stage-contract.json')==S['contract_sha256'][tid]
  for s in d['metadata']['slots']:Slot.model_validate(s)
def test_no_price_narrative_deviation_guess():
 d=e.read(P/'output/cc/stage-manifest.json')
 for t in ['key_tech_spec','key_tech_format','price_instructions']:assert d['documents'][t]['audit']['text_filled']==d['documents'][t]['audit']['images_filled']==0
 for t in ['price_summary','price_phase1','price_phase2','contractor_proposal','implementation_plan']:assert d['documents'][t]['audit']['text_filled']==0
@pytest.mark.parametrize('fail',['extract','semantic'])
def test_business_failure_fences_downstream(monkeypatch,fail):
 from tender_harness import workflow as w
 seen=[]
 async def execute(name,req,**kw):
  seen.append(req['stage']);assert kw['retry_policy'].maximum_attempts==1
  if req['stage']==fail:raise RuntimeError('controlled offline failure')
  return {'success':True,'source_sha256':'s','resources_sha256':'r'}
 monkeypatch.setattr(w.workflow,'execute_activity',execute)
 with pytest.raises(RuntimeError):asyncio.run(Workflow().run({'business_id':'OFFLINE','source_sha256':'s','resources_sha256':'r'}))
 assert seen==(['extract'] if fail=='extract' else ['extract','semantic'])

def test_independent_safety_c_required_not_b():
 from tender_harness.docx_ops.requirements import source_role_obligations
 d=source_role_obligations(ROOT/'inputs/original.docx',R,C['personnel_resume']);assert {(q['person_id'],q['level']) for q in d['required']}=={('P001','B'),('P003','B'),('P004','C')}
@pytest.mark.parametrize('change',['omit','wrong_level'])
def test_missing_safety_rule_caught_independently(change):
 from tender_harness.docx_ops.requirements import source_role_obligations
 c=copy.deepcopy(C['personnel_resume'])
 if change=='omit':del c['other_requirements']['P004']
 else:c['other_requirements']['P004'][0]['certificate_filter']['equals']['level']='B'
 with pytest.raises(e.b.GateError):source_role_obligations(ROOT/'inputs/original.docx',R,c)
def test_safety_c_current_exact_owner():
 xs,_=e.eligible_attachment(R,'P004','other_cert',C['personnel_resume'],M['plan']['other_requirements']);assert len(xs)==1 and e.current(xs[0]);cs={q['id']:q for q in R['native']['material_certificates']};q=cs[xs[0]['owner_id']];assert q['person_id']==R['records']['person:P004']['id'] and q['level']=='C' and q['expires_on']>=R['reference']['date']
