import threading
import unittest
from warden_drydock.hosted.engine.models import ExactTextChange, Status
from warden_drydock.hosted.proposals.service import InMemoryProposalRepository, ProposalService, ProposalStatus, ProposalVersion
from warden_drydock.hosted.revisions.models import StaleHeadError
from warden_drydock.hosted.revisions.models import FileHash, SnapshotManifest

def manifest(item, revision="revision_two"):
 return SnapshotManifest(item.campaign_id,revision,item.base_revision,2,"b"*64,(FileHash("record.md","c"*64),),"0.3.0","1.0.0","d"*64,item.diff_digest,"token_publish")

class Proposals(unittest.TestCase):
 def setUp(self):
  self.repo=InMemoryProposalRepository(); self.head='rev_one'; self.published=[]
  self.s=ProposalService(self.repo, head=lambda _:self.head, stage=lambda p:type('Stage',(),{'status':Status.STAGED})(), publish=lambda p,x:self.published.append((p,x)) or manifest(p), verify_publication=lambda value:value)
 def draft(self): return self.s.draft('proposal_one','campaign_one','rev_one',(ExactTextChange('change_one','record_one','a'*64,'# Two'),))
 def editor_item(self, proposal_id='proposal_editor', version=1, campaign_id='campaign_one', workflow_version=2, status=ProposalStatus.DRAFT, correction_of=None, diff_digest=None):
  changes=(ExactTextChange('change_%s_%d'%(proposal_id,version),'record_one','a'*64,'# Version %d'%version),)
  metadata={'editor_workflow_version':workflow_version}
  if correction_of is not None: metadata['correction_of']=correction_of
  return ProposalVersion(proposal_id,version,campaign_id,'rev_one',changes,diff_digest or self.s._diff_digest(changes),self.s._payload_digest(changes),status=status,editor_metadata=metadata)
 def test_correction_retires_old_and_binding_is_exact(self):
  old=self.draft(); new=self.s.correct(old,(ExactTextChange('change_two','record_one','a'*64,'# Three'),))
  self.assertEqual(ProposalStatus.REJECTED, self.repo.items[('proposal_one',1)].status)
  with self.assertRaises(ValueError): self.s.approve(old,diff_digest=old.diff_digest,base_revision=old.base_revision,payload_digest=old.payload_digest)
  with self.assertRaises(ValueError): self.s.approve(new,diff_digest='0'*64,base_revision=new.base_revision,payload_digest=new.payload_digest)
 def test_reject_is_idempotent_and_stale_conflicts(self):
  item=self.draft(); self.assertEqual(ProposalStatus.REJECTED,self.s.reject(item).status); self.assertEqual(ProposalStatus.REJECTED,self.s.reject(self.repo.items[('proposal_one',1)]).status)
  item=self.draft(); self.head='rev_other'; self.assertEqual(ProposalStatus.CONFLICT,self.s.approve(item,diff_digest=item.diff_digest,base_revision=item.base_revision,payload_digest=item.payload_digest).status); self.assertEqual([],self.published)
 def test_publish_once_and_crash_quarantines(self):
  item=self.draft(); self.assertEqual(ProposalStatus.PUBLISHED,self.s.approve(item,diff_digest=item.diff_digest,base_revision=item.base_revision,payload_digest=item.payload_digest).status); self.assertEqual(1,len(self.published))
  item=self.draft(); self.s._publish=lambda p,x: (_ for _ in ()).throw(RuntimeError('crash')); self.assertEqual(ProposalStatus.QUARANTINED,self.s.approve(item,diff_digest=item.diff_digest,base_revision=item.base_revision,payload_digest=item.payload_digest).status)
 def test_invalid_stage_and_second_approval_never_publish(self):
  item=self.draft(); self.s._stage=lambda p:type('Stage',(),{'status':Status.INVALID})(); self.assertEqual(ProposalStatus.DRAFT,self.s.approve(item,diff_digest=item.diff_digest,base_revision=item.base_revision,payload_digest=item.payload_digest).status); self.assertEqual([],self.published)
  self.s._stage=lambda p:type('Stage',(),{'status':Status.STAGED})(); first=self.s.approve(item,diff_digest=item.diff_digest,base_revision=item.base_revision,payload_digest=item.payload_digest); self.assertEqual(first,self.s.approve(item,diff_digest=item.diff_digest,base_revision=item.base_revision,payload_digest=item.payload_digest))
 def test_stage_exception_recovers_draft_and_claim_is_single_winner(self):
  item=self.draft(); self.s._stage=lambda p: (_ for _ in ()).throw(RuntimeError('stage')); self.assertEqual(ProposalStatus.DRAFT,self.s.approve(item,diff_digest=item.diff_digest,base_revision=item.base_revision,payload_digest=item.payload_digest).status)
  self.assertIsNotNone(self.repo.claim(item)); self.assertIsNone(self.repo.claim(item))
 def test_head_failure_recovers_and_conflict_can_rebase(self):
  item=self.draft(); self.s._head=lambda _: (_ for _ in ()).throw(RuntimeError('head')); self.assertEqual(ProposalStatus.DRAFT,self.s.approve(item,diff_digest=item.diff_digest,base_revision=item.base_revision,payload_digest=item.payload_digest).status)
  self.s._head=lambda _:'rev_other'; conflict=self.s.approve(item,diff_digest=item.diff_digest,base_revision=item.base_revision,payload_digest=item.payload_digest); corrected=self.s.correct(conflict,(ExactTextChange('change_three','record_one','a'*64,'# Four'),),base_revision='rev_other'); self.assertEqual((2,'rev_other',ProposalStatus.DRAFT),(corrected.version,corrected.base_revision,corrected.status))
 def test_stale_reject_cannot_rewrite_published_version(self):
  item=self.draft(); self.s.approve(item,diff_digest=item.diff_digest,base_revision=item.base_revision,payload_digest=item.payload_digest)
  self.assertRaises(ValueError,self.s.reject,item); self.assertEqual(ProposalStatus.PUBLISHED,self.repo.items[('proposal_one',1)].status)
 def test_approval_claim_excludes_concurrent_reject(self):
  item=self.draft(); self.assertIsNotNone(self.repo.claim(item)); self.assertRaises(ValueError,self.s.reject,item); self.assertEqual(ProposalStatus.APPROVING,self.repo.items[('proposal_one',1)].status)
  race=self.s.draft('proposal_race','campaign_one','rev_one',(ExactTextChange('change_race','record_one','a'*64,'# Two'),))
  barrier=threading.Barrier(2); claim_won=[]; reject_won=[]
  def claimant():
   barrier.wait(); claim_won.append(self.repo.claim(race) is not None)
  def rejector():
   barrier.wait()
   try: self.s.reject(race); reject_won.append(True)
   except ValueError: reject_won.append(False)
  claim_thread=threading.Thread(target=claimant); reject_thread=threading.Thread(target=rejector)
  claim_thread.start(); reject_thread.start(); claim_thread.join(10); reject_thread.join(10)
  self.assertFalse(claim_thread.is_alive()); self.assertFalse(reject_thread.is_alive())
  self.assertEqual(1,sum(claim_won)+sum(reject_won))
  self.assertEqual(ProposalStatus.APPROVING if claim_won[0] else ProposalStatus.REJECTED,self.repo.items[('proposal_race',1)].status)
 def test_approval_claim_excludes_concurrent_correction(self):
  def race(index,order):
   item=self.s.draft('proposal_race_%s_%d'%(order,index),'campaign_one','rev_one',(ExactTextChange('change_one','record_one','a'*64,'# Two'),))
   barrier=threading.Barrier(2); outcomes=[]
   def claim_side():
    barrier.wait(); outcomes.append(('claim',self.repo.claim(item)))
   def correct_side():
    barrier.wait()
    try: self.s.correct(item,(ExactTextChange('change_new','record_one','a'*64,'# New'),)); outcomes.append(('correct','accepted'))
    except ValueError: outcomes.append(('correct','refused'))
   threads=[threading.Thread(target=claim_side),threading.Thread(target=correct_side)]
   if order=='correct_first': threads.reverse()
   for thread in threads: thread.start()
   for thread in threads: thread.join(10)
   self.assertTrue(all(not thread.is_alive() for thread in threads))
   self.assertEqual(2,len(outcomes))
   return item,dict(outcomes)
  for order in ('claim_first','correct_first'):
   for index in range(25):
    item,outcomes=race(index,order)
    versions=tuple(sorted((v for v in self.repo.items.values() if v.proposal_id==item.proposal_id),key=lambda v:v.version))
    if outcomes['claim'] is not None:
     self.assertEqual('refused',outcomes['correct'])
     self.assertEqual((ProposalStatus.APPROVING,),tuple(v.status for v in versions))
    else:
     self.assertEqual('accepted',outcomes['correct'])
     self.assertEqual((ProposalStatus.REJECTED,ProposalStatus.DRAFT),tuple(v.status for v in versions))
     self.assertEqual(item.version+1,versions[1].version)
     self.assertEqual((ExactTextChange('change_new','record_one','a'*64,'# New'),),versions[1].changes)
 def test_private_paths_and_unsafe_change_ids_never_reach_audit(self):
  with self.assertRaises(ValueError): self.s.draft(r'C:\private\campaign.md','campaign_one','rev_one',(ExactTextChange('change_one','record_one','a'*64,'x'),))
  with self.assertRaises(ValueError): self.s.draft('proposal_safe','campaign_one','rev_one',(ExactTextChange('../private','record_one','a'*64,'x'),))
  self.assertEqual([],self.repo.audit)
 def test_generic_service_preserves_ordered_multi_change_proposals(self):
  changes=(ExactTextChange('change_first','record_one','a'*64,'# First'),
           ExactTextChange('change_second','record_two','b'*64,'# Second'))
  item=self.s.draft('proposal_multi','campaign_one','rev_one',changes)
  self.assertEqual(changes,item.changes)
  self.assertNotEqual(item.payload_digest,self.s.draft(
      'proposal_reverse','campaign_one','rev_one',tuple(reversed(changes))).payload_digest)
 def test_editor_add_rejects_campaign_and_workflow_mismatches_before_mutation(self):
  item=self.editor_item(status=ProposalStatus.PUBLISHED)
  self.assertFalse(self.repo.add_editor(item,'campaign_other',1))
  self.assertNotIn('campaign_one',self.repo._editor_workflow)
  wrong_stamp=self.editor_item(workflow_version=1)
  self.assertFalse(self.repo.add_editor(wrong_stamp,'campaign_one',1))
  stale=self.editor_item(workflow_version=3)
  self.assertFalse(self.repo.add_editor(stale,'campaign_one',2))
  self.assertNotIn('campaign_one',self.repo._editor_workflow)
  self.assertEqual({},self.repo.items)
  self.assertTrue(self.repo.add_editor(item,'campaign_one',1))
  self.assertEqual(ProposalStatus.DRAFT,self.repo.get(item.proposal_id,item.version).status)
  self.assertEqual((item.proposal_id,item.version,ProposalStatus.DRAFT.value),self.repo.audit[-1])
 def test_editor_add_rejects_version_gaps_and_orders_editor_reads(self):
  first=self.editor_item('proposal_zulu',workflow_version=2,status=ProposalStatus.PUBLISHED)
  self.assertTrue(self.repo.add_editor(first,'campaign_one',1))
  gap=self.editor_item('proposal_gap',version=3,workflow_version=3)
  with self.assertRaisesRegex(ValueError,'proposal_version_conflict'):
   self.repo.add_editor(gap,'campaign_one',2)
  second=self.editor_item('proposal_alpha',workflow_version=3,status=ProposalStatus.APPROVED)
  self.assertTrue(self.repo.add_editor(second,'campaign_one',2))
  self.assertEqual(('proposal_alpha','proposal_zulu'),tuple(item.proposal_id for item in self.repo.editor_proposals()))
  self.assertEqual((ProposalStatus.DRAFT,ProposalStatus.DRAFT),tuple(item.status for item in self.repo.editor_proposals()))
 def test_editor_correction_cannot_retire_missing_unrelated_or_non_editor_proposals(self):
  prior=self.editor_item(workflow_version=2)
  self.assertTrue(self.repo.add_editor(prior,'campaign_one',1))
  unrelated=self.editor_item('proposal_other',workflow_version=3)
  self.assertTrue(self.repo.add_editor(unrelated,'campaign_one',2))
  cross_proposal=self.editor_item('proposal_other',version=2,workflow_version=4,correction_of={'proposal_id':prior.proposal_id,'proposal_version':prior.version})
  self.assertFalse(self.repo.add_editor(cross_proposal,'campaign_one',3))
  self.assertEqual(ProposalStatus.DRAFT,self.repo.get(prior.proposal_id,prior.version).status)
  missing=self.editor_item(version=2,workflow_version=4,correction_of={'proposal_id':'proposal_missing','proposal_version':1})
  self.assertFalse(self.repo.add_editor(missing,'campaign_one',3))
  self.assertEqual(ProposalStatus.DRAFT,self.repo.get(prior.proposal_id,prior.version).status)
  plain=self.s.draft('proposal_plain','campaign_one','rev_one',(ExactTextChange('change_plain','record_one','a'*64,'# Plain'),))
  non_editor=self.editor_item('proposal_plain',version=2,workflow_version=4,correction_of={'proposal_id':plain.proposal_id,'proposal_version':plain.version})
  self.assertFalse(self.repo.add_editor(non_editor,'campaign_one',3))
  self.assertEqual(ProposalStatus.DRAFT,self.repo.get(plain.proposal_id,plain.version).status)
  self.assertNotIn((non_editor.proposal_id,non_editor.version),self.repo.items)
 def test_valid_editor_correction_retires_prior_and_preserves_metadata_in_correction(self):
  metadata={'editor_workflow_version':2,'marker':'prior'}
  prior= self.editor_item(workflow_version=2)
  prior=ProposalVersion(prior.proposal_id,prior.version,prior.campaign_id,prior.base_revision,prior.changes,prior.diff_digest,prior.payload_digest,editor_metadata=metadata)
  self.assertTrue(self.repo.add_editor(prior,'campaign_one',1))
  replacement=self.editor_item(version=2,workflow_version=3,correction_of={'proposal_id':prior.proposal_id,'proposal_version':prior.version})
  self.assertTrue(self.repo.add_editor(replacement,'campaign_one',2))
  self.assertEqual(ProposalStatus.REJECTED,self.repo.get(prior.proposal_id,prior.version).status)
  self.assertEqual(ProposalStatus.DRAFT,self.repo.get(replacement.proposal_id,replacement.version).status)
  corrected=self.s.correct(self.repo.get(replacement.proposal_id,replacement.version),(ExactTextChange('change_corrected','record_one','a'*64,'# Corrected'),))
  self.assertEqual(replacement.editor_metadata,corrected.editor_metadata)
  self.assertEqual(ProposalStatus.REJECTED,self.repo.get(replacement.proposal_id,replacement.version).status)
 def test_approve_finalize_is_safe_for_publishers_with_or_without_keyword(self):
  item=self.draft()
  callback=object()
  approved=self.s.approve(item,diff_digest=item.diff_digest,base_revision=item.base_revision,payload_digest=item.payload_digest,finalize=callback)
  self.assertEqual(ProposalStatus.PUBLISHED,approved.status)
  self.assertEqual(1,len(self.published))

  item=self.s.draft('proposal_finalize','campaign_one','rev_one',(ExactTextChange('change_finalize','record_one','a'*64,'# Finalize'),))
  received=[]
  def publish(version, staged, *, finalize):
   received.append(finalize)
   return manifest(version)
  self.s._publish=publish
  self.assertEqual(ProposalStatus.PUBLISHED,self.s.approve(item,diff_digest=item.diff_digest,base_revision=item.base_revision,payload_digest=item.payload_digest,finalize=callback).status)
  self.assertEqual([callback],received)
 def test_editor_approval_and_reconciliation_bind_stored_digest(self):
  item=self.editor_item(diff_digest='e'*64)
  self.assertTrue(self.repo.add_editor(item,'campaign_one',1))
  self.s._publish=lambda version, staged: None
  approved=self.s.approve(item,diff_digest=item.diff_digest,base_revision=item.base_revision,payload_digest=item.payload_digest)
  self.assertEqual(ProposalStatus.APPROVED,approved.status)
  reconciled=self.s.reconcile(approved,manifest(item))
  self.assertEqual(ProposalStatus.PUBLISHED,reconciled.status)
  self.assertEqual('revision_two',reconciled.published_revision_id)
