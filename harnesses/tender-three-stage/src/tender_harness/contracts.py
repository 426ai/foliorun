from typing import Literal,Annotated
from pydantic import BaseModel,ConfigDict,Field,StrictInt
Hash=Annotated[str,Field(pattern='^[a-f0-9]{64}$')]
class Closed(BaseModel):model_config=ConfigDict(extra='forbid')
class Requirement(Closed):
 title:str;clause_id:str;quote:str;selected_certificate_ids:list[str]
class Plan(Closed):
 source_sha256:Hash;resources_sha256:Hash;contracts_revision:Hash;template_ids:list[str];source_ranges:dict[str,list[StrictInt]];reason:Literal['ok'];rule_policy:Literal['all_declared'];person_ids:list[str];other_requirements:dict[str,Requirement]
class Command(Closed):
 operation:Literal['load','inspect','execute'];stage:Literal['extract','semantic','fill'];assets_bundle:str;prior_bundle:str;template_ids:list[str]=Field(default_factory=list);plan:Plan|None=None
class Artifact(Closed):name:str;path:str;sha256:Hash;size:StrictInt
class StageRequest(Closed):stage:Literal['extract','semantic','fill'];business_id:str;parent:dict|None=None
class StageResult(Closed):
 stage:Literal['extract','semantic','fill'];success:Literal[True];run_id:str;source_sha256:Hash;resources_sha256:Hash;manifest_sha256:Hash;artifacts:list[Artifact];actual_skill_read:Literal[True];gate:dict
class SourceRef(Closed):key:str;id:str;version:StrictInt;sha256:Hash
class Slot(Closed):
 tag:str;template_id:str;rule_id:str;label:str;kind:Literal['text','image'];owner_id:str|None=None;owner_type:str|None=None;identity_side:str|None=None;locator:dict|None=None;mandatory_declaration:bool=True;origin:Literal['base','generic','resume','unassigned_original_row'];mode:str|None=None;path:str|None=None;transform:str|None=None;source_refs:list[SourceRef]=Field(default_factory=list);hard_required:bool=False;base_slot:dict|None=None;person_alias:str|None=None;manual:bool=False;category:str|None=None;attachment_ids:list[str]=Field(default_factory=list);selection_reason:str|None=None
class Document(Closed):path:str;sha256:Hash;metadata:dict;audit:dict
class Combined(Closed):path:str;sha256:Hash;audit:dict
class Manifest(Closed):
 schema_id:Literal['full-stage/1']=Field(alias='schema');stage:Literal['extract','semantic','fill'];source_sha256:Hash;resources_sha256:Hash;contracts_revision:Hash;template_ids:list[str];documents:dict[str,Document];combined:Combined;parent_manifest_sha256:Hash|None;plan:Plan;all_stage_gates:Literal[True];formal_bid_ready:Literal[False]
