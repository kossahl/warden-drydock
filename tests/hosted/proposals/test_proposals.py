import unittest
from warden_drydock.hosted.engine.models import ExactTextChange, Status
from warden_drydock.hosted.proposals.service import InMemoryProposalRepository, ProposalService, ProposalStatus
from warden_drydock.hosted.revisions.models import StaleHeadError

class Proposals(unittest.TestCase):
 def setUp(self):
  self.repo=InMemoryProposalRepository(); self.head='rev_one'; self.published=[]
  self.s=ProposalService(self.repo, head=lambda _:self.head, stage=lambda p:type('Stage',(),{'status':Status.STAGED})(), publish=lambda p,x:self.published.append((p,x)) or 'revision_two')
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
  self.s._stage=lambda p:type('Stage',(),{'status':Status.STAGED})(); self.s.approve(item,diff_digest=item.diff_digest,base_revision=item.base_revision,payload_digest=item.payload_digest); self.assertRaises(ValueError,self.s.approve,item,diff_digest=item.diff_digest,base_revision=item.base_revision,payload_digest=item.payload_digest)
 def test_stage_exception_recovers_draft_and_claim_is_single_winner(self):
  item=self.draft(); self.s._stage=lambda p: (_ for _ in ()).throw(RuntimeError('stage')); self.assertEqual(ProposalStatus.DRAFT,self.s.approve(item,diff_digest=item.diff_digest,base_revision=item.base_revision,payload_digest=item.payload_digest).status)
  self.assertIsNotNone(self.repo.claim(item)); self.assertIsNone(self.repo.claim(item))
