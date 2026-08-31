import threading
import unittest
from warden_drydock.hosted.engine.models import ExactTextChange, Status
from warden_drydock.hosted.proposals.service import InMemoryProposalRepository, ProposalService, ProposalStatus
from warden_drydock.hosted.revisions.models import StaleHeadError
from warden_drydock.hosted.revisions.models import FileHash, SnapshotManifest

def manifest(item, revision="revision_two"):
 return SnapshotManifest(item.campaign_id,revision,item.base_revision,2,"b"*64,(FileHash("record.md","c"*64),),"0.3.0","1.0.0","d"*64,item.diff_digest,"token_publish")

class Proposals(unittest.TestCase):
 def setUp(self):
  self.repo=InMemoryProposalRepository(); self.head='rev_one'; self.published=[]
  self.s=ProposalService(self.repo, head=lambda _:self.head, stage=lambda p:type('Stage',(),{'status':Status.STAGED})(), publish=lambda p,x:self.published.append((p,x)) or manifest(p), verify_publication=lambda value:value)
 def draft(self): return self.s.draft('proposal_one','campaign_one','rev_one',(ExactTextChange('change_one','record_one','a'*64,'# Two'),))
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
