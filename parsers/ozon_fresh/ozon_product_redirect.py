"""Observe a public product redirect without following it or weakening access guards."""
from urllib.parse import urlsplit,parse_qs,quote
from copy import deepcopy
import ozon_fresh_fast as core
from ozon_har_catalog import canonical_product

class UnavailableProduct(core.ParseError):
    def __init__(self,message,item=None):
        super().__init__(message)
        self.item=deepcopy(item)

def unavailable_redirect(status,location,sku):
    if status!=302 or not location:return False
    try:
        target=urlsplit(core.public_url(location))
        if target.path!='/api/entrypoint-api.bx/page/json/v2':return False
        outer=parse_qs(target.query)
        if len(outer.get('url',[]))!=1:return False
        nested=urlsplit(outer['url'][0])
        query=parse_qs(nested.query)
        return (not nested.scheme and not nested.netloc and nested.path=='/search/' and
                query.get('product_id')==[str(sku)] and
                query.get('deny_category_prediction')==['true'] and query.get('from_global')==['true'])
    except (ValueError,core.ParseError):return False

def product_request(client,item,path=None):
    """CDP observes the already-issued request; no redirects are requested."""
    if not str(item.get('product_id','')).isdigit():
        raise core.ParseError('Product request requires an exact numeric SKU')
    canonical_product(item['url'],item['product_id'])
    parts=urlsplit(core.public_url(item['url']))
    request_path=path if path is not None else parts.path+('?' + parts.query if parts.query else '')
    expected=core.API+quote(request_path,safe='')
    session=client.page.context.new_cdp_session(client.page)
    tracked=set();responses={}
    def observe_response(request_id,response):
        if request_id not in tracked:return
        location=next((v for k,v in response.get('headers',{}).items() if k.casefold()=='location'),None)
        if location:responses[request_id]=(response.get('status',response.get('statusCode')),location)
    def request(event):
        if event['request'].get('url')==expected and event['request'].get('method')=='GET':tracked.add(event['requestId'])
        if event.get('redirectResponse'):observe_response(event['requestId'],event['redirectResponse'])
    def response(event):observe_response(event['requestId'],event['response'])
    def response_extra(event):observe_response(event['requestId'],event)
    session.on('Network.requestWillBeSent',request)
    session.on('Network.responseReceived',response)
    session.on('Network.responseReceivedExtraInfo',response_extra)
    session.send('Network.enable')
    error=None;result=None
    try:result=client.product_page(item['url']) if path is None else client.entrypoint(path)
    except core.ProtectionError as exc:error=exc
    finally:session.detach()
    if error is None:return result
    gate=getattr(client,'backoff_gate',None)
    ordinary_redirect=(str(error) in ('HTTP 0; increasing shared cooldown','HTTP 0 or redirect; stopped without retry')
        and client.state.get('last_status')==0 and not (gate and gate.manual_since)
        and any(unavailable_redirect(status,location,item['product_id']) for status,location in responses.values()))
    if ordinary_redirect:
        # This exact observed302 is a normal product-unavailable response.
        # No access-denial/manual state or lifetime error history is reset.
        client.state.update(blocked=False,previous_block_reason=client.state.pop('reason',None),
            product_unavailable_redirect={'status':302,'method':'GET','product_id':str(item['product_id']),
                'redirect_path':'/search/','observed_at':core.observed_now()})
        core.write_json(client.state_path,client.state)
        raise UnavailableProduct('published_same_sku_unavailable_redirect',item)
    raise error
