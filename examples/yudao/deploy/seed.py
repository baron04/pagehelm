"""Seed the local yudao deployment with synthetic business data through its admin API.

Every record goes through the application's own endpoints, so numbering,
validation and derived fields are the application's. The script is
idempotent: each record is looked up by its name (or number) first and only
created when missing, so it can be re-run against a partly seeded database.

All people, companies and amounts are synthetic.

Usage: seed.py [--base http://localhost:48088] [--tenant 1]
Environment: YUDAO_ADMIN_USERNAME / YUDAO_ADMIN_PASSWORD (default: the
upstream seed's local admin account), YUDAO_SEED_PASSWORD (password for the
seeded users; default is a local-only value).
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

RNG = random.Random(20260928)
NOW = datetime(2026, 9, 28, 10, 0, 0)


class ApiError(RuntimeError):
    def __init__(self, path: str, code, msg: str):
        super().__init__(f"{path}: {code} {msg}")
        self.code = code


class Client:
    def __init__(self, base: str, tenant: str, api: str = "admin-api"):
        self.base, self.tenant, self.token, self.api = base.rstrip("/"), tenant, None, api

    def call(self, method: str, path: str, body=None, query=None):
        url = f"{self.base}/{self.api}/{path}"
        if query:
            url += "?" + urllib.parse.urlencode(query, doseq=True)
        headers = {"tenant-id": self.tenant, "Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        with urllib.request.urlopen(req, timeout=60) as resp:
            out = json.load(resp)
        if out.get("code") != 0:
            raise ApiError(path, out.get("code"), out.get("msg", ""))
        return out.get("data")

    def get(self, path, **query):
        return self.call("GET", path, query=query)

    def post(self, path, body):
        return self.call("POST", path, body)

    def put(self, path, body=None, **query):
        return self.call("PUT", path, body, query=query or None)

    def login(self, username: str, password: str):
        self.token = self.post("system/auth/login", {"username": username, "password": password})["accessToken"]

    def page(self, path, **query):
        query = {"pageNo": 1, "pageSize": 100, **query}
        return self.get(path, **query)["list"]

    def find(self, path, key="name", value=None, **query):
        """First row of a page or list endpoint whose `key` equals value."""
        rows = self.get(path, **query)
        rows = rows["list"] if isinstance(rows, dict) else rows
        return next((r for r in rows if r.get(key) == value), None)

    def ensure(self, list_path, create_path, body, key="name", **query):
        """Return the id of the row named body[key], creating it when missing."""
        found = self.find(list_path, key, body[key], **query)
        if found:
            return found["id"], False
        return self.post(create_path, body), True


def day(offset_days: int, hour: int = 10) -> int:
    """Epoch milliseconds, the format the admin API accepts for LocalDateTime."""
    t = (NOW + timedelta(days=offset_days)).replace(hour=hour, minute=0, second=0)
    return int(t.timestamp() * 1000)


# --------------------------------------------------------------------- data

DEPTS = [  # name, parent name
    ("华东销售部", None), ("华南销售部", None), ("采购部", None), ("仓储物流部", None), ("财务部", None), ("生产部", None),
]
POSTS = [("销售经理", "sales_manager"), ("销售代表", "sales_rep"), ("采购专员", "buyer"), ("仓管员", "keeper"), ("会计", "accountant")]
USERS = [  # username, nickname, dept, post code, role codes
    ("zhangwei", "张伟", "华东销售部", "sales_manager", ["sales_manager"]),
    ("lina", "李娜", "华东销售部", "sales_rep", ["sales_rep"]),
    ("wangqiang", "王强", "华东销售部", "sales_rep", ["sales_rep"]),
    ("liuyang", "刘洋", "华南销售部", "sales_manager", ["sales_manager"]),
    ("chenjing", "陈静", "华南销售部", "sales_rep", ["sales_rep"]),
    ("zhaolei", "赵磊", "采购部", "buyer", ["buyer"]),
    ("sunli", "孙丽", "仓储物流部", "keeper", ["buyer"]),
    ("zhouming", "周明", "财务部", "accountant", ["accountant"]),
    ("evaluator", "评测员", "华东销售部", "sales_rep", ["sales_rep"]),
]
# Top-level menu paths each role may open; buttons below them come along.
ROLES = {
    "sales_manager": ("销售经理", ["/crm", "/erp", "/bpm"], 3),  # data scope 3: own dept and below
    "sales_rep": ("销售代表", ["/crm", "/erp", "/bpm"], 3),
    "buyer": ("采购专员", ["/erp", "/bpm"], 1),  # 1: all data
    "accountant": ("会计", ["/crm", "/erp", "/bpm", "/fms"], 1),
}

COMPANIES = [
    "杭州云帆科技有限公司", "苏州瑞达精密机械有限公司", "上海恒信贸易有限公司", "南京博远信息技术有限公司", "宁波海川电子有限公司",
    "无锡新拓自动化设备有限公司", "合肥明德教育科技有限公司", "广州南方智造有限公司", "深圳鹏程物联科技有限公司", "东莞华盛塑胶制品有限公司",
    "佛山美居家具有限公司", "厦门海湾食品有限公司", "福州闽江建材有限公司", "长沙湘江医疗器械有限公司", "武汉光谷光电有限公司",
    "成都天府软件有限公司", "重庆山城汽配有限公司", "西安古城文化传媒有限公司", "青岛蓝海冷链物流有限公司", "济南泉城化工有限公司",
    "天津滨海港务服务有限公司", "北京中关村数据有限公司", "郑州中原农业科技有限公司", "昆明春城花卉有限公司",
]
PEOPLE = ["周杰", "吴敏", "郑浩", "冯雪", "蒋涛", "沈佳", "韩梅", "杨帆", "朱琳", "秦峰", "许诺", "何静", "吕斌", "施然", "孔颖", "曹宇",
          "严华", "金鑫", "魏东", "陶然", "姜岚", "戚明", "谢婷", "邹凯", "喻辉", "柏雪", "水清", "窦言", "章程", "云帆"]
CRM_PRODUCTS = [  # name, no, unit dict value, price
    ("企业版订阅（年）", "CP-001", 1, 29800), ("专业版订阅（年）", "CP-002", 1, 9800), ("实施服务（人天）", "CP-003", 1, 2500),
    ("定制开发（人天）", "CP-004", 1, 3200), ("培训服务（场）", "CP-005", 1, 6000), ("硬件网关", "CP-006", 1, 4500),
]
ERP_PRODUCTS = [  # name, category, unit, purchase price, sale price
    ("工业网关 G100", "电子设备", "台", 820, 1280), ("温湿度传感器 T20", "电子设备", "个", 45, 89), ("PLC 控制器 P5", "电子设备", "台", 2300, 3600),
    ("光电开关 E3", "电子设备", "个", 38, 72), ("伺服电机 S750", "机械部件", "台", 1650, 2480), ("减速机 R60", "机械部件", "台", 980, 1520),
    ("直线导轨 L30", "机械部件", "根", 260, 410), ("轴承 6205", "机械部件", "个", 12, 25), ("防护手套", "劳保用品", "双", 6, 15),
    ("安全帽", "劳保用品", "顶", 28, 55), ("工作服", "劳保用品", "套", 85, 160), ("包装纸箱 60cm", "包装材料", "个", 4, 9),
    ("气泡膜 1m", "包装材料", "卷", 32, 58), ("打包带", "包装材料", "卷", 18, 35),
]


# --------------------------------------------- what a module is read under

ACCOUNT_SET = "示例信息技术有限公司"
OFFICIAL_ACCOUNT = "示例公众号"
MAIL_PROVIDER = "示例邮箱服务"
MAIL_ACCOUNT = "finance@example.com"


# ------------------------------------------------------------------- phases

def short(product_name: str) -> str:
    return product_name.split("（")[0]


def seed_org(c: Client, password: str) -> dict:
    ids = {"dept": {}, "user": {}, "role": {}, "post": {}}
    root = c.get("system/dept/list")
    root_id = next(d["id"] for d in root if d["parentId"] == 0)
    for i, (name, parent) in enumerate(DEPTS):
        ids["dept"][name], _ = c.ensure("system/dept/list", "system/dept/create",
                                        {"name": name, "parentId": ids["dept"].get(parent, root_id), "sort": 10 + i, "status": 0})
    for i, (name, code) in enumerate(POSTS):
        found = c.find("system/post/page", "code", code, pageNo=1, pageSize=100)
        ids["post"][code] = found["id"] if found else c.post("system/post/create", {"name": name, "code": code, "sort": 10 + i, "status": 0})

    menus = c.get("system/menu/simple-list")
    by_parent: dict[int, list] = {}
    for m in menus:
        by_parent.setdefault(m["parentId"], []).append(m)

    def subtree(menu_id):
        out = [menu_id]
        for ch in by_parent.get(menu_id, []):
            out += subtree(ch["id"])
        return out

    tops = {m.get("path"): m["id"] for m in c.get("system/menu/list") if m["parentId"] == 0}
    for i, (code, (name, paths, scope)) in enumerate(ROLES.items()):
        found = c.find("system/role/page", "code", code, pageNo=1, pageSize=100)
        rid = found["id"] if found else c.post("system/role/create", {"name": name, "code": code, "sort": 10 + i, "status": 0})
        ids["role"][code] = rid
        menu_ids = sorted({x for p in paths if p in tops for x in subtree(tops[p])})
        c.post("system/permission/assign-role-menu", {"roleId": rid, "menuIds": menu_ids})
        c.post("system/permission/assign-role-data-scope", {"roleId": rid, "dataScope": scope, "dataScopeDeptIds": []})

    for username, nickname, dept, post, roles in USERS:
        found = c.find("system/user/page", "username", username, username=username)
        uid = found["id"] if found else c.post("system/user/create", {
            "username": username, "nickname": nickname, "deptId": ids["dept"][dept], "postIds": [ids["post"][post]],
            "password": password, "sex": 1, "mobile": f"139{RNG.randint(10000000, 99999999)}",
            "email": f"{username}@example.com"})
        ids["user"][username] = uid
        c.post("system/permission/assign-user-role", {"userId": uid, "roleIds": [ids["role"][r] for r in roles]})
    # Department leaders.
    for dept, leader in (("华东销售部", "zhangwei"), ("华南销售部", "liuyang"), ("采购部", "zhaolei"), ("财务部", "zhouming")):
        d = next(x for x in c.get("system/dept/list") if x["name"] == dept)
        if d.get("leaderUserId") != ids["user"][leader]:
            c.put("system/dept/update", {**{k: d[k] for k in ("id", "name", "parentId", "sort", "status")}, "leaderUserId": ids["user"][leader]})
    ids["admin"] = c.get("system/auth/get-permission-info")["user"]["id"]
    print(f"org: {len(ids['dept'])} depts, {len(ids['user'])} users, {len(ids['role'])} roles")
    return ids


def seed_bpm(c: Client, org: dict, password: str):
    """Deploy the approval processes CRM and OA submit into, one approver each."""
    manager = Client(c.base, c.tenant)
    manager.login("zhangwei", password)
    category = c.find("bpm/category/page", "code", "crm", pageNo=1, pageSize=100)
    if not category:
        c.post("bpm/category/create", {"name": "客户管理", "code": "crm", "status": 0, "sort": 1})
    oa = c.find("bpm/category/page", "code", "oa", pageNo=1, pageSize=100)
    if not oa:
        c.post("bpm/category/create", {"name": "办公审批", "code": "oa", "status": 0, "sort": 2})

    def simple(approver_id: int, label: str):
        return {"id": "StartUserNode", "type": 10, "name": "发起人", "showText": "已设置",
                "childNode": {"id": "Activity_approve", "type": 11, "name": label, "showText": label,
                              "candidateStrategy": 30, "candidateParam": str(approver_id), "approveType": 1,
                              "approveMethod": 4, "rejectHandler": {"type": 1}, "timeoutHandler": {"enable": False},
                              "assignEmptyHandler": {"type": 1}, "assignStartUserHandlerType": 1,
                              "childNode": {"id": "EndEvent", "type": 1, "name": "结束"}}}

    models = [
        ("crm-contract-audit", "合同审批", "crm", "/crm/contract/detail", org["user"]["zhangwei"]),
        ("crm-receivable-audit", "回款审批", "crm", "/crm/receivable/detail", org["user"]["zhouming"]),
        ("oa_leave", "请假审批", "oa", "/bpm/oa/leave/create", org["user"]["zhangwei"]),
    ]
    existing = {m["key"]: m for m in c.get("bpm/model/list")}
    for key, name, cat, path, approver in models:
        if key in existing and existing[key].get("processDefinition"):
            continue
        body = {"key": key, "name": name, "category": cat, "type": 20, "formType": 20, "formCustomCreatePath": path,
                "formCustomViewPath": path, "visible": True, "managerUserIds": [org["admin"], org["user"]["zhangwei"]],
                "simpleModel": simple(approver, f"{name}人")}
        mid = existing[key]["id"] if key in existing else c.post("bpm/model/create", body)
        # A model is editable only by its managers; fall back to the first manager.
        for client in (c, manager):
            try:
                if key in existing:
                    client.put("bpm/model/update", {**body, "id": mid})
                client.post(f"bpm/model/deploy?id={mid}", None)
                break
            except ApiError as e:
                if e.code != 1009002007 or client is manager:
                    raise
    print("bpm: contract, receivable and leave approvals deployed")


def seed_crm(c: Client, org: dict):
    users = org["user"]
    sales = ["zhangwei", "lina", "wangqiang", "liuyang", "chenjing", "evaluator"]

    # Product catalogue.
    cat = c.find("crm/product-category/list", "name", "软件与服务")
    cat_id = cat["id"] if cat else c.post("crm/product-category/create", {"name": "软件与服务", "parentId": 0})
    products = {}
    for name, no, unit, price in CRM_PRODUCTS:
        found = c.find("crm/product/page", "no", no, no=no)
        products[name] = (found["id"] if found else c.post("crm/product/create", {
            "name": name, "no": no, "unit": unit, "price": price, "status": 1, "categoryId": cat_id,
            "ownerUserId": users["zhangwei"]}), price)

    # Business status group.
    groups = c.page("crm/business-status/page")
    group = next((g for g in groups if g["name"] == "标准销售流程"), None)
    if group:
        status_type_id = group["id"]
    else:
        status_type_id = c.post("crm/business-status/create", {"name": "标准销售流程", "deptIds": [], "statuses": [
            {"name": "初步接洽", "percent": 10}, {"name": "需求确认", "percent": 30}, {"name": "方案报价", "percent": 60},
            {"name": "商务谈判", "percent": 80}]})
    statuses = c.get("crm/business-status/get", id=status_type_id)["statuses"]

    # Clues: some remain clues, some are the origin of customers.
    for i in range(30):
        name = f"{PEOPLE[i % len(PEOPLE)]}（{COMPANIES[(i * 7) % len(COMPANIES)][:4]}咨询）"
        c.ensure("crm/clue/page", "crm/clue/create", {
            "name": name, "ownerUserId": users[sales[i % len(sales)]], "mobile": f"138{10000000 + i * 7919:08d}"[:11],
            "source": 1 + i % 6, "level": 1 + i % 3, "industryId": 1 + i % 8,
            "contactNextTime": day(3 + i % 20), "remark": "展会获取" if i % 3 == 0 else "官网咨询"}, name=name)

    customers = {}
    for i, company in enumerate(COMPANIES):
        owner = sales[i % len(sales)]
        cid, _ = c.ensure("crm/customer/page", "crm/customer/create", {
            "name": company, "ownerUserId": users[owner], "industryId": 1 + i % 8, "level": 1 + i % 3,
            "source": 1 + i % 6, "mobile": f"137{20000000 + i * 104729:08d}"[:11], "detailAddress": f"{company[:2]}市高新区{i + 1}号",
            "contactNextTime": day(1 + i % 14), "remark": "重点客户" if i % 5 == 0 else ""}, name=company)
        # yudao makes whoever creates a customer its owner, whatever the request says:
        # hand it to its salesperson, as a manager reassigning an account would.
        current = next(r for r in c.page("crm/customer/page", name=company) if r["id"] == cid)
        if current.get("ownerUserId") != users[owner]:
            c.put("crm/customer/transfer", {"id": cid, "newOwnerUserId": users[owner]})
        customers[company] = (cid, owner)

    contacts = {}
    for i, (company, (cid, owner)) in enumerate(customers.items()):
        for j in range(1 + (i % 3 == 0)):
            name = PEOPLE[(i * 2 + j) % len(PEOPLE)] + ("" if j == 0 else "（采购）")
            found = next((r for r in c.page("crm/contact/page", name=name) if r["customerId"] == cid), None)
            contacts.setdefault(company, []).append(found["id"] if found else c.post("crm/contact/create", {
                "name": name, "customerId": cid, "ownerUserId": users[owner], "mobile": f"136{30000000 + i * 31 + j:08d}"[:11],
                "master": j == 0, "post": "总经理" if j == 0 else "采购经理", "sex": 1 + (i + j) % 2}))

    product_list = list(products.items())
    businesses = {}
    for i, company in enumerate(list(customers)[:18]):
        cid, owner = customers[company]
        pname, (pid, price) = product_list[i % len(product_list)]
        count = 1 + i % 5
        name = f"{company[:4]}{short(pname)}项目"
        found = c.find("crm/business/page", "name", name, name=name)
        businesses[company] = found["id"] if found else c.post("crm/business/create", {
            "name": name, "customerId": cid, "ownerUserId": users[owner], "statusTypeId": status_type_id,
            "dealTime": day(10 + i * 3), "discountPercent": 0, "contactId": contacts[company][0],
            "products": [{"productId": pid, "productPrice": price, "businessPrice": price, "count": count}]})
        if not found and i % 4 != 0:
            c.put("crm/business/update-status", {"id": businesses[company], "statusId": statuses[min(i % 4, len(statuses) - 1)]["id"]})

    contracts = []
    for i, company in enumerate(list(customers)[:15]):
        cid, owner = customers[company]
        items = [product_list[(i + k) % len(product_list)] for k in range(1 + i % 2)]
        name = f"{company[:4]}{short(items[0][0])}合同"
        found = c.find("crm/contract/page", "name", name, name=name)
        if found:
            contracts.append((found["id"], cid, owner, found.get("totalPrice")))
            continue
        lines = [{"productId": pid, "productPrice": price, "contractPrice": price, "count": 1 + (i + k) % 3}
                 for k, (_, (pid, price)) in enumerate(items)]
        total = sum(l["contractPrice"] * l["count"] for l in lines)
        kid = c.post("crm/contract/create", {
            "name": name, "customerId": cid, "businessId": businesses.get(company), "ownerUserId": users[owner],
            "orderDate": day(-60 + i * 4), "startTime": day(-55 + i * 4), "endTime": day(300 + i * 4),
            "discountPercent": 0, "totalPrice": total, "signContactId": contacts[company][0],
            "signUserId": users[owner], "products": lines})
        contracts.append((kid, cid, owner, total))
        if i % 5 != 4:  # most contracts go to approval; every fifth stays a draft
            c.put("crm/contract/submit", id=kid)
    print(f"crm: {len(customers)} customers, {len(businesses)} opportunities, {len(contracts)} contracts")
    return {"customers": customers, "contracts": contracts, "products": products}


def seed_receivables(c: Client, org: dict, crm: dict):
    """Plans for approved contracts, and receipts against the earliest plans."""
    users = org["user"]
    approved = [k for k in c.page("crm/contract/page", pageSize=100) if k["auditStatus"] == 20]
    receipts = 0
    for i, k in enumerate(sorted(approved, key=lambda k: k["id"])):
        plans = c.page("crm/receivable-plan/page", contractId=k["id"])
        if not plans:
            half = round(float(k["totalPrice"]) / 2, 2)
            for n, (offset, amount) in enumerate(((-20 + i * 3, half), (40 + i * 3, round(float(k["totalPrice"]) - half, 2)))):
                c.post("crm/receivable-plan/create", {
                    "contractId": k["id"], "customerId": k["customerId"], "ownerUserId": k["ownerUserId"],
                    "returnTime": day(offset), "returnType": 1 + (i + n) % 3, "price": amount, "remindDays": 7})
            plans = c.page("crm/receivable-plan/page", contractId=k["id"])
        first = min(plans, key=lambda p: p["period"])
        if i % 3 == 2 or c.page("crm/receivable/page", planId=first["id"]):
            continue  # every third approved contract has nothing received yet
        rid = c.post("crm/receivable/create", {
            "ownerUserId": k["ownerUserId"], "customerId": k["customerId"], "contractId": k["id"], "planId": first["id"],
            "returnType": first.get("returnType") or 1, "price": first["price"], "returnTime": day(-15 + i * 3)})
        c.put("crm/receivable/submit", id=rid)
        receipts += 1
    print(f"crm: receivable plans for {len(approved)} approved contracts, {receipts} new receipts")


def seed_erp(c: Client, org: dict):
    users = org["user"]
    units = {}
    for name in sorted({u for _, _, u, _, _ in ERP_PRODUCTS}):
        units[name], _ = c.ensure("erp/product-unit/page", "erp/product-unit/create", {"name": name, "status": 0}, name=name)
    cats = {}
    for i, name in enumerate(sorted({cat for _, cat, _, _, _ in ERP_PRODUCTS})):
        cats[name], _ = c.ensure("erp/product-category/list", "erp/product-category/create",
                                 {"name": name, "parentId": 0, "code": f"C{i + 1:02d}", "sort": i, "status": 0}, name=name)
    products = {}
    for i, (name, cat, unit, buy, sell) in enumerate(ERP_PRODUCTS):
        products[name], _ = c.ensure("erp/product/page", "erp/product/create", {
            "name": name, "barCode": f"69{20260000 + i:010d}", "categoryId": cats[cat], "unitId": units[unit], "status": 0,
            "standard": name.split(" ")[-1], "purchasePrice": buy, "salePrice": sell, "minPrice": round(sell * 0.85, 2)}, name=name)
    warehouses = {}
    for i, (name, addr) in enumerate((("杭州总仓", "杭州市余杭区仓前街道"), ("广州分仓", "广州市黄埔区科学城"), ("苏州备件仓", "苏州市工业园区"))):
        warehouses[name], _ = c.ensure("erp/warehouse/page", "erp/warehouse/create",
                                       {"name": name, "address": addr, "sort": i, "status": 0, "principal": "孙丽"}, name=name)
    account, _ = c.ensure("erp/account/page", "erp/account/create", {"name": "招商银行基本户", "no": "6225880000001234", "status": 0, "sort": 0}, name="招商银行基本户")
    suppliers = []
    for i, company in enumerate(COMPANIES[4:12]):
        sid, _ = c.ensure("erp/supplier/page", "erp/supplier/create", {
            "name": company, "contact": PEOPLE[i + 10], "mobile": f"135{40000000 + i * 17:08d}", "status": 0, "sort": i,
            "taxPercent": 13}, name=company)
        suppliers.append(sid)
    customers = []
    for i, company in enumerate(COMPANIES[12:22]):
        cid, _ = c.ensure("erp/customer/page", "erp/customer/create", {
            "name": company, "contact": PEOPLE[i + 5], "mobile": f"133{50000000 + i * 23:08d}", "status": 0, "sort": i,
            "taxPercent": 13}, name=company)
        customers.append(cid)
    names = list(products)
    wh = list(warehouses.values())

    def lines(i, price_key):
        picked = [names[(i * 3 + k) % len(names)] for k in range(1 + i % 3)]
        spec = {n: p for n, *p in [(x[0], x[3], x[4]) for x in ERP_PRODUCTS]}
        return [{"productId": products[n], "productUnitId": units[next(u for m, _, u, _, _ in ERP_PRODUCTS if m == n)],
                 "productPrice": spec[n][0 if price_key == "buy" else 1], "count": 10 * (1 + (i + k) % 5), "taxPercent": 13}
                for k, n in enumerate(picked)]

    # Purchases: approve most, receive most approved ones into stock.
    for i in range(12):
        tag = f"种子-P{i + 1:02d}"
        found = c.page("erp/purchase-order/page", remark=tag)
        if found:
            continue
        oid = c.post("erp/purchase-order/create", {"supplierId": suppliers[i % len(suppliers)], "accountId": account,
                                                    "orderTime": day(-80 + i * 5), "discountPercent": 0, "remark": tag, "items": lines(i, "buy")})
        if i % 6 == 5:
            continue  # stays unapproved
        c.put("erp/purchase-order/update-status", id=oid, status=20)
        if i % 4 == 3:
            continue  # approved, not yet received
        order = c.get("erp/purchase-order/get", id=oid)
        iid = c.post("erp/purchase-in/create", {"orderId": oid, "accountId": account, "inTime": day(-75 + i * 5), "discountPercent": 0, "otherPrice": 0,
                                                "remark": tag, "items": [{"orderItemId": it["id"], "warehouseId": wh[i % 2],
                                                                          "productId": it["productId"], "productUnitId": it["productUnitId"],
                                                                          "productPrice": it["productPrice"], "count": it["count"], "taxPercent": it.get("taxPercent")}
                                                                         for it in order["items"]]})
        c.put("erp/purchase-in/update-status", id=iid, status=20)

    def stock_of(product_id):
        return max((float(r["count"]) for r in c.page("erp/stock/page", productId=product_id)), default=0.0)

    def stocked(product_id):
        """The warehouse holding most of a product, so shipments come from real stock."""
        rows = c.page("erp/stock/page", productId=product_id)
        return max(rows, key=lambda r: float(r["count"]))["warehouseId"] if rows else wh[0]

    # Sales: approve most, ship some from stock.
    for i in range(15):
        tag = f"种子-S{i + 1:02d}"
        if c.page("erp/sale-order/page", remark=tag):
            continue
        items = [dict(l, count=max(1, l["count"] // 5)) for l in lines(i + 1, "sell")]
        oid = c.post("erp/sale-order/create", {"customerId": customers[i % len(customers)], "accountId": account,
                                                "saleUserId": users[["zhangwei", "lina", "wangqiang", "liuyang", "chenjing"][i % 5]],
                                                "orderTime": day(-50 + i * 3), "discountPercent": 0, "remark": tag, "items": items})
        if i % 5 == 4:
            continue
        c.put("erp/sale-order/update-status", id=oid, status=20)
        if i % 3 == 2:
            continue
        order = c.get("erp/sale-order/get", id=oid)
        if any(stock_of(it["productId"]) < float(it["count"]) for it in order["items"]):
            continue  # approved but not shippable from current stock
        try:
            out = c.post("erp/sale-out/create", {"orderId": oid, "accountId": account, "outTime": day(-45 + i * 3), "discountPercent": 0, "otherPrice": 0,
                                                 "remark": tag, "items": [{"orderItemId": it["id"], "warehouseId": stocked(it["productId"]),
                                                                           "productId": it["productId"], "productUnitId": it["productUnitId"],
                                                                           "productPrice": it["productPrice"], "count": it["count"], "taxPercent": it.get("taxPercent")}
                                                                          for it in order["items"]]})
            c.put("erp/sale-out/update-status", id=out, status=20)
        except ApiError as e:
            print(f"erp: {tag} not shipped ({e})")
    print(f"erp: {len(products)} products, {len(suppliers)} suppliers, {len(customers)} customers, 12 purchases, 15 sales")


def seed_leave(c: Client, password: str):
    """Leave requests from the sales team; the department leader approves most."""
    for i, username in enumerate(["lina", "wangqiang", "evaluator", "chenjing", "lina"]):
        u = Client(c.base, c.tenant)
        u.login(username, password)
        reason = f"种子-L{i + 1:02d} " + ["年假", "病假", "事假", "调休", "年假"][i]
        if any(r.get("reason") == reason for r in u.page("bpm/oa/leave/page")):
            continue
        u.post("bpm/oa/leave/create", {"type": 1 + i % 3, "reason": reason,
                                        "startTime": day(5 + i * 7, 9), "endTime": day(6 + i * 7, 18)})


MALL = {  # second-level category -> [(name, price in yuan, stock)]
    "网关": [("边缘网关 E1", 1299, 80), ("4G 工业路由器 R2", 699, 120)],
    "传感器": [("温湿度传感器 T20", 89, 500), ("烟雾传感器 S8", 129, 300), ("门磁传感器 D3", 49, 600)],
    "配件": [("导轨电源 24V", 159, 200), ("RS485 转换器", 69, 400), ("防水接线盒", 35, 800)],
}
MEMBERS = [("15600000001", "浙江省杭州市西湖区文三路 90 号"), ("15600000002", "江苏省苏州市工业园区星湖街 328 号"),
           ("15600000003", "上海市浦东新区张江路 500 号"), ("15600000004", "广东省深圳市南山区科技园"),
           ("15600000005", "湖北省武汉市洪山区光谷大道 77 号"), ("15600000006", "四川省成都市高新区天府大道 1 号")]
AREA_IDS = [330106, 320571, 310115, 440305, 420111, 510107]
PIC = "http://localhost:48088/favicon.ico"  # placeholder image; the local deployment has no file storage configured


def sms_login(member: "Client", mobile: str) -> str:
    """Log a member in with the local deployment's fixed SMS code (sms-code.begin-code = 9999).

    Codes are single-use and limited to one per mobile per minute, so a code
    sent less than a minute ago means waiting once.
    """
    for attempt in range(2):
        try:
            member.post("member/auth/send-sms-code", {"mobile": mobile, "scene": 1})
            break
        except ApiError as e:
            if e.code != 1002014005 or attempt:
                raise
            time.sleep(61)
    return member.post("member/auth/sms-login", {"mobile": mobile, "code": "9999"})["accessToken"]


def seed_mall(c: Client):
    # Payment: the mall pays through the "mall" app with the mock channel, notified back to the server itself.
    apps = c.page("pay/app/page", appKey="mall")
    app_id = apps[0]["id"] if apps else c.post("pay/app/create", {
        "appKey": "mall", "name": "商城", "status": 0,
        "orderNotifyUrl": "http://127.0.0.1:48080/app-api/trade/order/update-paid",
        "refundNotifyUrl": "http://127.0.0.1:48080/admin-api/trade/after-sale/update-refunded"})
    if not c.call("GET", "pay/channel/get", query={"appId": app_id, "code": "mock"}):
        c.post("pay/channel/create", {"appId": app_id, "code": "mock", "status": 0, "feeRate": 0, "config": json.dumps({"name": "mock"})})

    # Charges are matched by the address's district, so the template lists the members' districts.
    template = {"name": "全国包邮", "chargeMode": 1, "sort": 0, "frees": [],
                "charges": [{"areaIds": AREA_IDS, "startCount": 1, "startPrice": 0, "extraCount": 1, "extraPrice": 0}]}
    tpl, created = c.ensure("trade/delivery/express-template/page", "trade/delivery/express-template/create", template, name="全国包邮")
    if not created and not c.get("trade/delivery/express-template/get", id=tpl).get("charges"):
        c.put("trade/delivery/express-template/update", {**template, "id": tpl})
    express = c.find("trade/delivery/express/page", "name", "顺丰速运", pageNo=1, pageSize=100)
    express_id = express["id"] if express else c.post("trade/delivery/express/create",
                                                       {"code": "SF", "name": "顺丰速运", "logo": PIC, "sort": 0, "status": 0})
    brand, _ = c.ensure("product/brand/page", "product/brand/create",
                        {"name": "云帆智能", "picUrl": PIC, "sort": 0, "status": 0}, name="云帆智能", pageNo=1, pageSize=100)
    root, _ = c.ensure("product/category/list", "product/category/create",
                       {"name": "智能硬件", "parentId": 0, "picUrl": PIC, "sort": 0, "status": 0}, name="智能硬件")
    skus = {}
    for i, (cat_name, items) in enumerate(MALL.items()):
        cat, _ = c.ensure("product/category/list", "product/category/create",
                          {"name": cat_name, "parentId": root, "picUrl": PIC, "sort": i, "status": 0}, name=cat_name)
        for j, (name, price, stock) in enumerate(items):
            found = c.find("product/spu/page", "name", name, name=name, tabType=0, pageNo=1, pageSize=100) \
                or c.find("product/spu/page", "name", name, name=name, tabType=1, pageNo=1, pageSize=100)
            spu = found["id"] if found else c.post("product/spu/create", {
                "name": name, "keyword": cat_name, "introduction": f"{name}，工业级品质", "description": f"<p>{name}</p>",
                "categoryId": cat, "brandId": brand, "picUrl": PIC, "sliderPicUrls": [PIC], "sort": i * 10 + j,
                "specType": False, "deliveryTypes": [1], "deliveryTemplateId": tpl, "giveIntegral": 0,
                "subCommissionType": False, "virtualSalesCount": 0, "salesCount": 0, "browseCount": 0,
                "skus": [{"name": name, "price": price * 100, "marketPrice": int(price * 120), "costPrice": price * 60,
                          "picUrl": PIC, "stock": stock, "properties": []}]})
            if not found:
                c.put("product/spu/update-status", {"id": spu, "status": 1})
            skus[name] = c.get("product/spu/get-detail", id=spu)["skus"][0]["id"]

    sku_names = list(skus)
    orders = 0
    for m, (mobile, address) in enumerate(MEMBERS):
        tags = [f"种子-M{m + 1:02d}-{k + 1}" for k in range(2 + m % 2)]
        known = c.page("member/user/page", mobile=mobile)
        if known and set(tags) <= {o.get("userRemark") for o in c.page("trade/order/page", userId=known[0]["id"])}:
            continue  # this member's orders are all present; no login needed
        member = Client(c.base, c.tenant, api="app-api")
        member.token = sms_login(member, mobile)
        if not known:
            known = c.page("member/user/page", mobile=mobile)
            c.put("member/user/update", {"id": known[0]["id"], "mobile": mobile, "nickname": PEOPLE[m + 3], "status": 0})
        addresses = member.get("member/address/list")
        addr = addresses[0]["id"] if addresses else member.post("member/address/create", {
            "name": PEOPLE[m + 3], "mobile": mobile, "areaId": AREA_IDS[m], "detailAddress": address, "defaultStatus": True})
        existing = {o.get("userRemark") for o in member.page("trade/order/page")}
        for k, tag in enumerate(tags):
            if tag in existing:
                continue
            items = [{"skuId": skus[sku_names[(m * 3 + k + x) % len(sku_names)]], "count": 1 + (m + x) % 3} for x in range(1 + k % 2)]
            order = member.post("trade/order/create", {"items": items, "pointStatus": False, "deliveryType": 1,
                                                       "addressId": addr, "remark": tag})
            orders += 1
            state = (m * 3 + k) % 5  # 0 unpaid, 1 paid, 2-3 shipped, 4 received
            if state == 0:
                continue
            member.post("pay/order/submit", {"id": order["payOrderId"], "channelCode": "mock"})
            for _ in range(20):  # the mock channel notifies the trade module asynchronously
                if member.get("trade/order/get-detail", id=order["id"])["payStatus"]:
                    break
                time.sleep(0.5)
            if state >= 2:
                c.put("trade/order/delivery", {"id": order["id"], "logisticsId": express_id, "logisticsNo": f"SF{1000000 + m * 10 + k}"})
            if state == 4:
                member.put("trade/order/receive", id=order["id"])
    print(f"mall: {len(skus)} products, {len(MEMBERS)} members, {orders} new orders")


def seed_parents(c: Client):
    """The record every other record of a module is read under: the account set a ledger
    and a voucher belong to, the official account a mall page shows, the mail account the
    mail pages read, the employee record a person's own HR pages open.

    A module without its parent cannot be used at all -- its pages show an empty selector
    and, where the page asks the server for the parent's records, answer nothing -- so the
    fixture carries exactly one of each: what the application itself selects. Accounting
    is initialized as well as created, because an account set that is not initialized has
    no chart of accounts and its pages refuse.
    """
    sets = c.get("fms/config/account-set/list") or []
    held = next((s for s in sets if s.get("companyName") == ACCOUNT_SET), None)
    if held is None:
        c.post("fms/config/account-set/create", {
            "companyCode": "WOA-DEMO", "companyName": ACCOUNT_SET, "industry": "信息技术服务",
            "location": "杭州", "legalRepresentative": "张伟", "contactName": "张伟",
            "mobile": "13900000001", "email": "finance@example.com", "address": "杭州市西湖区示例路 1 号"})
        held = next(s for s in c.get("fms/config/account-set/list") if s["companyName"] == ACCOUNT_SET)
    if not held.get("initialized"):
        try:
            c.put("fms/config/account-set/initialize", {
                "accountSetId": held["id"], "currencyCode": "RMB",
                "startTime": int(datetime(2026, 1, 1, 0, 0, 0).timestamp() * 1000),
                "standard": 1, "level": 4, "subjectCodeRule": "4-2-2-2", "ledgerBalanceMode": 1})
        except ApiError as e:
            # Initializing builds the chart of accounts from `fms_subject_template` and the
            # closing templates' subjects. Upstream publishes that reference data outside
            # the MIT repositories, so a database without it cannot initialize: the account
            # set and every list read under it still work, but its subject and closing pages
            # stay empty. Load it with subject_chart.py.
            print(f"parents: the account set stays uninitialized ({e}); "
                  "load subject_chart.py to give it a chart of accounts")

    if not any(a.get("name") == OFFICIAL_ACCOUNT for a in c.get("mp/account/list-all-simple") or []):
        c.post("mp/account/create", {
            "name": OFFICIAL_ACCOUNT, "account": "woa-demo", "appId": "wx" + _hex(16),
            "appSecret": _hex(32), "token": _hex(16), "remark": "本地测试账号（合成数据）"})

    providers = c.get("oa/mail-provider/list") or []
    provider = next((p for p in providers if p.get("name") == MAIL_PROVIDER), None)
    provider_id = provider["id"] if provider else c.post("oa/mail-provider/create", {
        "name": MAIL_PROVIDER, "status": 0,
        "imap": {"host": "imap.example.com", "port": 993, "sslEnable": True, "starttlsEnable": False},
        "smtp": {"host": "smtp.example.com", "port": 465, "sslEnable": True, "starttlsEnable": False}})
    if not any(a.get("mail") == MAIL_ACCOUNT for a in c.get("oa/mail-account/list") or []):
        c.post("oa/mail-account/create", {
            "providerId": provider_id, "mail": MAIL_ACCOUNT, "username": MAIL_ACCOUNT,
            "password": _hex(16), "defaultStatus": True, "status": 0})

    # An employee record per person the fixture knows, so their own HR pages open: the
    # portal reads the signed-in person's employee record, and refuses without one.
    employees = {e.get("userId") for e in c.page("hrm/employee/page")}
    for username, nickname, dept, post, _roles in USERS:
        user = c.find("system/user/page", "username", username, username=username)
        if user is None or user["id"] in employees:
            continue
        c.post("hrm/employee/create", {
            "name": nickname, "userId": user["id"], "deptId": user["deptId"],
            "jobNumber": f"E{user['id']:04d}", "mobile": user.get("mobile") or f"138{RNG.randint(10000000, 99999999)}",
            "sex": 1, "entryStatus": 1, "status": 1, "type": 1, "entryTime": day(-180, 9), "probation": 0,
            "postName": next((n for n, code in POSTS if code == post), ""),
            "email": f"{username}@example.com", "workCity": "杭州"})
    admin = c.get("system/auth/get-permission-info")["user"]
    if admin["id"] not in employees:
        c.post("hrm/employee/create", {
            "name": admin.get("nickname") or admin["username"], "userId": admin["id"],
            "deptId": admin.get("deptId") or 0, "jobNumber": f"E{admin['id']:04d}",
            "mobile": f"138{RNG.randint(10000000, 99999999)}", "sex": 1,
            "entryStatus": 1, "status": 1, "type": 1, "entryTime": day(-180, 9), "probation": 0})

    # 薪资：建薪资配置，它同时开出第一张工资表；员工工资明细按工资表读取。
    if c.get("hrm/salary/config/get") is None:
        c.post("hrm/salary/config/create", {
            "cycleStartDay": 1, "socialSecurityMonthType": 1, "startYear": 2026, "startMonth": 1})
    print(f"parents: {len(c.get('fms/config/account-set/list') or [])} account set, "
          f"{len(c.get('mp/account/list-all-simple') or [])} official account, "
          f"{len(c.get('oa/mail-account/list') or [])} mail account, "
          f"{len(c.page('hrm/employee/page'))} employees, "
          f"{c.get('hrm/salary/month-record/page', pageNo=1, pageSize=100)['total']} payroll records")


def _hex(length: int) -> str:
    """A synthetic identifier of n hex characters (a WeChat app id, key or token)."""
    return "".join(RNG.choice("0123456789abcdef") for _ in range(length))


def approve_all(c: Client, org: dict, password: str, approver: str, keep_pending: int, process_key: str):
    """Approve all but the newest `keep_pending` tasks of one process as the approver."""
    approver_client = Client(c.base, c.tenant)
    approver_client.login(approver, password)
    tasks = approver_client.page("bpm/task/todo-page", pageSize=100)
    tasks = [t for t in tasks if (t.get("processInstance") or {}).get("processDefinitionId", "").startswith(process_key + ":")]
    tasks.sort(key=lambda t: t["createTime"])
    for t in tasks[: max(0, len(tasks) - keep_pending)]:
        approver_client.put("bpm/task/approve", {"id": t["id"], "reason": "同意"})
    return len(tasks)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:48088")
    ap.add_argument("--tenant", default="1")
    ap.add_argument("--only", nargs="*", default=None,
                    help="run only these phases (matched against their names)")
    args = ap.parse_args()
    c = Client(args.base, args.tenant)
    c.login(os.environ.get("YUDAO_ADMIN_USERNAME", "admin"), os.environ.get("YUDAO_ADMIN_PASSWORD", "admin123"))
    password = os.environ.get("YUDAO_SEED_PASSWORD", "Seed-local-2026")

    def wanted(phase: str) -> bool:
        return args.only is None or phase in args.only

    if wanted("parents"):
        # Every phase below is additive on top of these, but none needs them; a run that
        # wants only the parents stops here.
        seed_parents(c)
    if not wanted("org"):
        return
    org = seed_org(c, password)
    if wanted("bpm"):
        seed_bpm(c, org, password)
    if wanted("crm"):
        crm = seed_crm(c, org)
        if wanted("receivables"):
            seed_receivables(c, org, crm)
    if wanted("approvals"):
        n = approve_all(c, org, password, "zhangwei", keep_pending=4, process_key="crm-contract-audit")
        print(f"bpm: {n} pending contract approvals found, all but the newest 4 approved")
        n = approve_all(c, org, password, "zhouming", keep_pending=2, process_key="crm-receivable-audit")
        print(f"bpm: {n} pending receipt approvals found, all but the newest 2 approved")
    if wanted("erp"):
        seed_erp(c, org)
    if wanted("leave"):
        seed_leave(c, password)
        n = approve_all(c, org, password, "zhangwei", keep_pending=2, process_key="oa_leave")
        print(f"bpm: {n} pending leave approvals found, all but the newest 2 approved")
    if wanted("mall"):
        seed_mall(c)


if __name__ == "__main__":
    try:
        main()
    except ApiError as e:
        sys.exit(f"seed failed: {e}")
