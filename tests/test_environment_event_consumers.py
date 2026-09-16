import asyncio
from app.core import environment_events as ee

def test_unconsumed_receipt():
    ee._workflow_consumers.clear(); ee._workflow_receipts.clear()
    ee.publish_workflow_event({'kind':'helper_ready_to_resume','trace_id':'t'})
    assert ee.workflow_consumer_receipts()[-1]['status']=='unconsumed'

def test_sync_consumer_dispatch():
    ee._workflow_consumers.clear(); ee._workflow_receipts.clear(); seen=[]
    ee.register_workflow_consumer('helper_ready_to_resume', lambda p: seen.append(p['trace_id']))
    ee.publish_workflow_event({'kind':'helper_ready_to_resume','trace_id':'t'})
    assert seen==['t']; assert ee.workflow_consumer_receipts()[-1]['status']=='dispatched'

async def _acb(p): pass

def test_async_consumer_dispatch():
    ee._workflow_consumers.clear(); ee._workflow_receipts.clear()
    ee.register_workflow_consumer('*', _acb)
    asyncio.run(_run())
    assert ee.workflow_consumer_receipts()[-1]['status']=='dispatched'
async def _run():
    ee.publish_workflow_event({'kind':'x','trace_id':'t'})
    await asyncio.sleep(0)
