"""Render JSON with the production browser engine for existing UI assertions.

Unlike TestClient.get, rendered_pages follows every bounded list page so lifecycle
assertions can locate fixtures without depending on accumulated database order.
HTTP shell, pagination and isolation contracts are tested separately.
"""
import json
import re
import subprocess
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, parse_qsl
import httpx

PATHS={"/admin","/admin/api-keys","/admin/workers","/admin/sessions","/admin/users",
       "/admin/finance","/admin/reports","/admin/google","/admin/dchat","/user/overview",
       "/user/account","/user/workers","/user/debug"}

def rendered_pages(client, url, **kwargs):
    parts=urlsplit(str(url))
    if parts.path not in PATHS and not parts.path.startswith("/user/usage/"):
        return client.get(url, **kwargs)
    data_url=urlunsplit(("", "", parts.path+"/data", parts.query,""))
    response=client.get(data_url, **kwargs)
    if response.status_code!=200 or "application/json" not in response.headers.get("content-type",""):
        return response
    data=response.json()
    pages=[data]
    params=dict(parse_qsl(parts.query))
    params.update(kwargs.get("params",{}))
    for field in ["pagination","subscription_pagination","user_pagination","worker_pagination"]:
        if field not in data: continue
        meta=data[field]
        for number in range(2,meta["pages"]+1):
            options={**kwargs,"params":{**params,meta["parameter"]:number}}
            item=client.get(parts.path+"/data",**options)
            assert item.status_code==200,item.text
            pages.append(item.json())
    result=subprocess.run(["node",str(Path(__file__).with_name("page_render.cjs"))],
        input=json.dumps({"url":"http://testserver"+str(url),"pages":pages,"lang":response.headers.get("content-language", "zh-CN")}),capture_output=True,text=True,check=True)
    html="\n".join(json.loads(result.stdout))
    shell=client.get(url,**kwargs)
    sidebar=re.search(r"<aside.*?</aside>",shell.text,re.S)
    if sidebar: html=html.replace("</body>",sidebar.group(0)+"</body>")
    return httpx.Response(200,content=html,headers={"content-type":"text/html"},request=response.request)
