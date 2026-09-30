"""Seed the local yudao deployment with the third-batch scale-up data (seed-v3).

Additive: it runs on top of the seed-v2 database and only creates new records,
through the admin API (and, for follow-ups and collaborator rows, the record
owner's own session, which the application requires). It never changes the
schema, never edits or deletes a seeded-v1/v2 record, and is idempotent: every
record is looked up by its exact name, number or tag first and only created
when missing; approvals only act on what is genuinely pending or drafted.

All people, companies and amounts are synthetic. Dates hang off the fixed
anchor NOW (2026-09-29), so the same run writes the same values anywhere.

Usage: seed_v3.py [--base http://localhost:48088] [--tenant 1]
Environment: YUDAO_ADMIN_USERNAME / YUDAO_ADMIN_PASSWORD (default: the local
admin account), YUDAO_SEED_PASSWORD (password of the seeded users; default is
a local-only value).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import zlib
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

NOW = datetime(2026, 9, 29, 10, 0, 0)


class ApiError(RuntimeError):
    def __init__(self, path: str, code, msg: str):
        super().__init__(f"{path}: {code} {msg}")
        self.code = code


class Client:
    def __init__(self, base: str, tenant: str):
        self.base, self.tenant, self.token = base.rstrip("/"), tenant, None

    def call(self, method: str, path: str, body=None, query=None):
        url = f"{self.base}/admin-api/{path}"
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
        self.token = self.post("system/auth/login",
                               {"username": username, "password": password})["accessToken"]

    def page(self, path, **query):
        query = {"pageNo": 1, "pageSize": 100, **query}
        return self.get(path, **query)["list"]

    def pages(self, path, **query):
        page, out = 1, []
        while True:
            rows = self.page(path, pageNo=page, **query)
            out += rows
            if len(rows) < 100:
                return out
            page += 1

    def find(self, path, key="name", value=None, **query):
        rows = self.get(path, **query)
        rows = rows["list"] if isinstance(rows, dict) else rows
        return next((r for r in rows if r.get(key) == value), None)

    def try_put(self, path, body=None, **query):
        """A PUT that tolerates the application's own already-there rejection."""
        try:
            self.put(path, body, **query)
            return True
        except ApiError as e:
            print(f"{path} skipped: {e}")
            return False


def day(offset_days: int, hour: int = 10) -> int:
    """Epoch milliseconds, the format the admin API accepts for LocalDateTime."""
    t = (NOW + timedelta(days=offset_days)).replace(hour=hour, minute=0, second=0)
    return int(t.timestamp() * 1000)


# ----------------------------------------------------------------- vocabularies

CITIES = ["嘉兴", "湖州", "绍兴", "台州", "金华", "温州", "南通", "常州", "徐州", "扬州", "盐城",
          "镇江", "泰州", "宿迁", "连云港", "淮安", "昆山", "太仓", "常熟", "张家港", "余姚",
          "慈溪", "平湖", "临安", "德清", "长兴"]
BRANDS = ["星辰", "旭日", "蓝湾", "瑞丰", "恒昌", "泰和", "中科", "慧云", "金泽", "绿洲",
          "天成", "沃德", "启明", "翔宇", "宏图", "锦程", "博纳", "力源", "海纳", "睿见"]
SUFFIXES = ["科技有限公司", "智能设备有限公司", "软件技术有限公司", "电子信息有限公司",
            "自动化工程有限公司", "数据服务有限公司"]
PEOPLE = ["高翔", "罗兰", "戴鑫", "冉旭", "殷涛", "蒲雪", "郗云", "樊星", "汪顺", "屈岚",
          "米佳", "贝宁", "仰杰", "欧洋", "时雨", "穆怀", "山岚", "向晚", "单于", "连城",
          "闻迪", "阿宝", "集睿", "成彦", "展鹏", "捷阳", "璧合", "朔方", "常晴", "初阳",
          "格致", "清风", "浩然", "明轩", "砚青", "桃夭", "榆光", "听澜", "煜宁", "岚山"]

OWNER_CYCLE = ["evaluator", "lina", "evaluator", "wangqiang", "evaluator", "zhangwei", "liuyang", "chenjing"]

# Extra CRM products: name, no, unit, price, status (1 on, 2 off).
CRM_PRODUCTS_V3 = [
    ("专业版订阅（两年）", "CP-101", 1, 19600, 1), ("专业版订阅（三年）", "CP-102", 1, 27000, 1),
    ("企业版订阅（两年）", "CP-103", 1, 56000, 1), ("企业版订阅（三年）", "CP-104", 1, 80000, 1),
    ("数据迁移服务（批）", "CP-105", 1, 8000, 1), ("安全审计服务（次）", "CP-106", 1, 12000, 1),
    ("云存储扩容（T/年）", "CP-107", 1, 3600, 1), ("接口开发包（套）", "CP-108", 1, 5200, 1),
    ("移动端插件（个）", "CP-109", 1, 2800, 1), ("报表模板包（套）", "CP-110", 1, 4600, 1),
    ("运维托管（年）", "CP-111", 1, 36000, 1), ("灾备演练（次）", "CP-112", 1, 9000, 1),
    ("性能调优（人天）", "CP-113", 1, 3000, 1), ("驻场支持（人月）", "CP-114", 1, 42000, 1),
    ("边缘计算盒子", "CP-115", 1, 6800, 1), ("工业相机 C50", "CP-116", 1, 7400, 1),
    ("条码扫描枪 B20", "CP-117", 1, 1300, 1), ("4G 数据卡（年）", "CP-118", 1, 600, 1),
    ("机柜（台）", "CP-119", 1, 2100, 1), ("UPS 电源（台）", "CP-120", 1, 3300, 1),
    ("培训包（进阶）", "CP-121", 1, 15000, 1), ("定制报表（张）", "CP-122", 1, 1800, 1),
    ("BI 看板模板（套）", "CP-123", 1, 9600, 1), ("消息推送网关（年）", "CP-124", 1, 7200, 1),
    ("电子签章（次）", "CP-125", 1, 2000, 1), ("发票识别包（千张）", "CP-126", 1, 1500, 1),
    ("银企直连（年）", "CP-127", 1, 24000, 1), ("税控接口（年）", "CP-128", 1, 9800, 1),
    ("多语言包（套）", "CP-129", 1, 6600, 2), ("语音客服模块（年）", "CP-130", 1, 28800, 1),
    ("工单系统模块（年）", "CP-131", 1, 16800, 1), ("知识库模块（年）", "CP-132", 1, 12600, 1),
    ("物联网接入点（个）", "CP-133", 1, 480, 1), ("视频汇聚服务（路）", "CP-134", 1, 3600, 1),
    ("数字孪生场景（个）", "CP-135", 1, 32000, 1), ("低代码扩展包（套）", "CP-136", 1, 21000, 2),
    ("短信包（万条）", "CP-137", 1, 4000, 1), ("数据大屏（块）", "CP-138", 1, 18000, 1),
    ("移动巡检终端（台）", "CP-139", 1, 5400, 1), ("安全网关（台）", "CP-140", 1, 15800, 2),
    ("测试环境租用（月）", "CP-141", 1, 6000, 2), ("架构咨询（人天）", "CP-142", 1, 8800, 1),
]
V2_PRODUCT_NAMES = ["企业版订阅（年）", "专业版订阅（年）", "实施服务（人天）",
                    "定制开发（人天）", "培训服务（场）", "硬件网关"]

# Customers deliberately named near another record:
#  A 苏州蓝湾 x2, both visible          -> "苏州蓝湾" must ask which
#  B exact name + branch, both visible  -> even the full name matches two
#  C exact visible + branch invisible   -> answering the visible one directly is right
#  D near-name, different characters    -> 上海衡信 answers; 上海恒信 (v2, not visible) does not
SPECIAL_CUSTOMERS = [
    ("苏州蓝湾智能设备有限公司", "evaluator"), ("苏州蓝湾电子信息有限公司", "evaluator"),
    ("宁波恒昌泰和化工有限公司", "evaluator"), ("宁波恒昌泰和化工有限公司舟山分公司", "evaluator"),
    ("杭州旭日东方财富有限公司", "evaluator"), ("杭州旭日东方财富有限公司滨江分公司", "liuyang"),
    ("上海衡信商贸有限公司", "evaluator"),
    ("温州天成食品有限公司", "evaluator"), ("厦门瑞丰软件有限公司", "evaluator"),
    ("广州宏图数据服务有限公司", "evaluator"), ("深圳市鹏城物联科技有限公司", "evaluator"),
]
# v2 customers the evaluator already owns; reused read-only, never modified.
V2_EVALUATOR_CUSTOMERS = ["无锡新拓自动化设备有限公司", "厦门海湾食品有限公司",
                          "西安古城文化传媒有限公司", "昆明春城花卉有限公司"]


# ------------------------------------------------------------------ phases

def ensure_crm_admin(c: Client, admin_id: int) -> None:
    """Give the seeding admin the application's own crm_admin role.

    The CRM aspect (CrmPermissionAspect) and the sceneType page filter honour
    role code crm_admin (RoleCodeEnum.CRM_ADMIN): only then do the admin's list
    lookups see records already transferred to a colleague, which is what makes
    re-running this script idempotent instead of duplicating them. Created
    through the management API; the seeded salespeople are untouched.
    """
    role = c.find("system/role/page", "code", "crm_admin")
    if role is None:
        c.post("system/role/create", {"name": "CRM 管理员", "code": "crm_admin",
                                      "sort": 10, "dataScope": 1, "status": 0})
        role = c.find("system/role/page", "code", "crm_admin")
    have = c.get("system/permission/list-user-roles", userId=admin_id) or []
    if role["id"] not in have:
        c.post("system/permission/assign-user-role", {"userId": admin_id, "roleIds": list(have) + [role["id"]]})


def seed_products(c: Client, owner: int) -> dict:
    out = {}
    for name in V2_PRODUCT_NAMES:  # the v2 catalogue, read-only lookups
        row = c.find("crm/product/page", "name", name, name=name)
        if row:
            out[name] = (row["id"], float(row["price"]))
    category = (c.find("crm/product-category/list", "name", "软件与服务")
                or c.find("crm/product-category/list", "name", "产品分类"))
    new = 0
    for name, no, unit, price, status in CRM_PRODUCTS_V3:
        # the page endpoint's `no` filter does not match; look up by exact name
        row = c.find("crm/product/page", "name", name, name=name)
        if not row:
            pid = c.post("crm/product/create", {
                "name": name, "no": no, "unit": unit, "price": price, "status": status,
                "categoryId": category["id"], "ownerUserId": owner})
            row = c.get("crm/product/get", id=pid)
            new += 1
        out[name] = (row["id"], float(row["price"]))
    print(f"products: {len(out)} known ({new} new this run)", flush=True)
    return out


def v3_customer_names() -> list:
    """Deterministic (name, planned_owner) plan; identical on every run so a
    resumed run touches exactly the same rows."""
    names = []
    for i in range(250):
        name = (f"{CITIES[i % len(CITIES)]}{BRANDS[(i * 3 + i // len(CITIES)) % len(BRANDS)]}"
                f"{SUFFIXES[(i * 5 + i // 11) % len(SUFFIXES)]}")
        assert name not in {n for n, _ in names}, name
        names.append((name, OWNER_CYCLE[i % 8]))
    names += SPECIAL_CUSTOMERS
    # Near-name clusters: sibling companies sharing a city+brand prefix, both
    # under the evaluator, so "打开X那家公司" is a genuine 2-way ambiguity.
    # Combos are picked from prefixes the generated plan never uses, so each
    # cluster is exactly two names; first 8 such (city, brand) pairs.
    taken = {n[:4] for n, _ in names}
    clusters = 0
    for ci, city in enumerate(CITIES):
        if clusters >= 8:
            break
        for bj, brand in enumerate(BRANDS):
            if (ci + bj) % 3 != 1 or city + brand in taken:
                continue
            s1, s2 = SUFFIXES[(ci + 2 * bj) % 6], SUFFIXES[(ci + 2 * bj + 2) % 6]
            names.append((f"{city}{brand}{s1}", "evaluator"))
            names.append((f"{city}{brand}{s2}", "evaluator"))
            taken.add(city + brand)
            clusters += 1
            break
    assert clusters >= 8, clusters
    return names


def pool_target_names(owners: dict) -> list:
    """Same subset seed_pool moves on a fresh run, computed from the plan,
    not from current ownership -- idempotent across resumed runs."""
    lina = [n for n, o in v3_customer_names() if o == "lina"]
    return [n for n in lina[7::8]]


def seed_customers(c: Client, owners: dict) -> dict:
    names = v3_customer_names()
    customers, current, created = {}, {}, 0
    for i, (name, owner_name) in enumerate(names):
        row = next((r for r in c.page("crm/customer/page", name=name) if r["name"] == name), None)
        if row is None:
            cid = c.post("crm/customer/create", {
                "name": name, "ownerUserId": owners[owner_name],
                "industryId": 1 + i % 8, "level": 1 + i % 3, "source": 1 + i % 6,
                "mobile": f"137{str(60000000 + i * 31337)[-8:]}",
                "detailAddress": f"{name[:2]}市产业新城{1 + i % 99}号",
                "contactNextTime": day(1 + i % 30), "remark": ""})
            row = c.get("crm/customer/get", id=cid)
            created += 1
        # Downstream name generation must be identical on a resumed run, so the
        # plan (not the freshly read ownership) is what goes into the dict;
        # `current` only drives the transfer pass below.
        customers[name] = (row["id"], owners[owner_name])
        current[name] = row.get("ownerUserId")
    # yudao forces whoever creates a customer to be its owner, so hand every
    # v3 record over to its planned owner. v2 rows are not in this list.
    for name, owner_name in names:
        cid = customers[name][0]
        assert cid is not None, name
        cur = current[name]
        if cur == owners[owner_name] or cur is None:
            # cur None = deliberate public-pool end state (seed_pool); a pooled
            # row is not re-transferred on a resumed run.
            continue
        c.put("crm/customer/transfer", {"id": cid, "newOwnerUserId": owners[owner_name]})
    for name in V2_EVALUATOR_CUSTOMERS:  # reuse v2 rows read-only
        row = c.find("crm/customer/page", "name", name, name=name)
        if row:
            customers.setdefault(name, (row["id"], row.get("ownerUserId")))
    print(f"customers: {len(customers)} keyed ({created} new this run)", flush=True)
    return customers


def seed_clues(c: Client, owners: dict):
    sales = ["evaluator", "lina", "wangqiang", "zhangwei", "chenjing", "liuyang"]
    new = 0
    for i in range(90):
        name = f"{PEOPLE[i % len(PEOPLE)]}（{BRANDS[(i + i // len(PEOPLE)) % len(BRANDS)]}咨询V3）"
        if c.find("crm/clue/page", "name", name, name=name):
            continue
        c.post("crm/clue/create", {
            "name": name, "ownerUserId": owners[sales[i % len(sales)]],
            "mobile": f"138{str(70000000 + i * 104729)[-8:]}",
            "industryId": 1 + i % 8, "level": 1 + i % 3, "source": 1 + i % 6,
            "contactNextTime": day(2 + i % 25), "remark": "线上咨询" if i % 2 else "展会获取"})
        new += 1
    print(f"clues: {new} new this run", flush=True)


def seed_contacts(c: Client, customers: dict, owners: dict) -> dict:
    """One to three contacts per v3 customer, plus the 张伟 near-name cluster."""
    contacts = {}
    items = [(n, v) for n, v in customers.items() if n not in V2_EVALUATOR_CUSTOMERS]
    new = 0
    for i, (company, (cid, owner)) in enumerate(items):
        wanted, seen = [], set()
        for nm in (PEOPLE[i % len(PEOPLE)], PEOPLE[(i * 2 + 5) % len(PEOPLE)],
                   PEOPLE[(i * 3 + 11) % len(PEOPLE)]):
            if nm not in seen:
                seen.add(nm)
                wanted.append(nm)
        for j, cname in enumerate(wanted):
            row = next((r for r in c.page("crm/contact/page", name=cname, customerId=cid)
                        if r["name"] == cname and r["customerId"] == cid), None)
            if row is None:
                kid = c.post("crm/contact/create", {
                    "name": cname, "customerId": cid, "ownerUserId": owner or 1,
                    "mobile": f"136{str(80000000 + (i * 31 + j) * 7919)[-8:]}",
                    # the special-contact block below gives these two companies
                    # a master=1 张伟; keep exactly one master per company
                    "master": j == 0 and company not in ("温州天成食品有限公司",
                                                          "广州宏图数据服务有限公司"),
                    "post": "采购经理" if j else "总经理"})
                row = {"id": kid, "name": cname, "customerId": cid}
                new += 1
            contacts[(company, cname)] = row["id"]
    for company, cname, master, mobile in [
            ("温州天成食品有限公司", "张伟", 1, "13611112001"),
            ("温州天成食品有限公司", "张伟明", 0, "13611112002"),
            ("广州宏图数据服务有限公司", "张伟", 1, "13611112003"),
            ("厦门瑞丰软件有限公司", "张大伟", 0, "13611112004")]:
        cid = customers[company][0]
        row = next((r for r in c.page("crm/contact/page", name=cname, customerId=cid)
                    if r["name"] == cname and r["customerId"] == cid), None)
        if row is None:
            kid = c.post("crm/contact/create", {"name": cname, "customerId": cid,
                                                "ownerUserId": owners["evaluator"],
                                                "mobile": mobile, "master": bool(master),
                                                "post": "技术负责人"})
            row = {"id": kid, "name": cname, "customerId": cid}
            new += 1
        contacts[(company, cname)] = row["id"]
    print(f"contacts: {len(contacts)} keyed ({new} new this run)", flush=True)
    return contacts


def seed_businesses(c: Client, customers: dict, contacts: dict, owners: dict, products: dict) -> dict:
    groups = c.page("crm/business-status/page")
    group = next(g for g in groups if g["name"] == "标准销售流程")
    statuses = c.get("crm/business-status/get", id=group["id"])["statuses"]
    plist = list(products.items())
    pool = [(n, v) for n, v in customers.items()
            if v[1] in (owners["evaluator"], owners["lina"], owners["wangqiang"], owners["zhangwei"])]
    businesses, new, used = {}, 0, {}
    for i in range(200):
        company, (cid, owner) = pool[i % len(pool)]
        pname, (pid, price) = plist[(i * 5) % len(plist)]
        short = pname.split("（")[0]
        base = f"{company[:4]}{short}项目"
        used[base] = used.get(base, 0)
        name = base if used[base] == 0 else f"{base}（{used[base] + 1}期）"
        used[base] += 1
        contact_id = next((v for (cc, _p), v in contacts.items() if cc == company), None)
        found = c.find("crm/business/page", "name", name, name=name)
        if found:
            businesses[name] = found["id"]
            continue
        bid = c.post("crm/business/create", {
            "name": name, "customerId": cid, "ownerUserId": owner, "statusTypeId": group["id"],
            "dealTime": day(-90 + (i * 7) % 300), "discountPercent": 0, "contactId": contact_id,
            "products": [{"productId": pid, "productPrice": price, "businessPrice": price,
                          "count": 1 + i % 4}]})
        new += 1
        kind = i % 10
        if kind in (1, 7):
            c.try_put("crm/business/update-status", {
                "id": bid, "statusId": statuses[2 + (i % 2)]["id"], "endStatus": 1 if kind == 1 else 2})
        elif kind != 0:
            c.try_put("crm/business/update-status", {"id": bid, "statusId": statuses[i % 4]["id"]})
        businesses[name] = bid
    for suffix, company in (("", "苏州蓝湾智能设备有限公司"), ("二期", "苏州蓝湾电子信息有限公司")):
        name = f"蓝湾数字孪生项目{suffix}"
        cid = customers[company][0]
        pid, price = products["数字孪生场景（个）"]
        found = c.find("crm/business/page", "name", name, name=name)
        bid = found["id"] if found else c.post("crm/business/create", {
            "name": name, "customerId": cid, "ownerUserId": owners["evaluator"],
            "statusTypeId": group["id"], "dealTime": day(20 if not suffix else 55),
            "discountPercent": 0,
            "products": [{"productId": pid, "productPrice": price, "businessPrice": price, "count": 2}]})
        businesses[name] = bid
    print(f"businesses: {len(businesses)} keyed ({new} new this run)", flush=True)
    return businesses


def seed_contracts(c: Client, customers: dict, contacts: dict, owners: dict,
                   products: dict) -> list:
    """Returns [(id, name, owner_user_id, want_reject)] for every v3 contract."""
    plist = list(products.items())
    eval_c = [k for k, v in customers.items() if v[1] == owners["evaluator"]]
    team_c = [k for k, v in customers.items()
              if v[1] in (owners["lina"], owners["wangqiang"], owners["zhangwei"])]
    away_c = [k for k, v in customers.items() if v[1] in (owners["liuyang"], owners["chenjing"])]
    specs, used = [], {}
    for i in range(195):
        pool = eval_c if i % 10 < 6 else (team_c if i % 10 < 9 else away_c)
        company = pool[(i * 7) % len(pool)]
        pname, (pid, price) = plist[(i * 3) % len(plist)]
        short = pname.split("（")[0]
        base = f"{company[:4]}{short}合同V3"
        used[base] = used.get(base, 0)
        name = base if used[base] == 0 else f"{base}（{used[base] + 1}次）"
        used[base] += 1
        submit = i % 20 != 3
        reject = submit and i % 13 == 4
        specs.append((name, company, customers[company][1], submit, reject, pid, price))
    # Specials go first so their approval tasks are the OLDEST in BPM: the
    # keep_pending rule then leaves the last 26 bulk contracts pending and
    # always approves the named specials (their tasks drive plan-shaped truth).
    specs = [
        ("厦门瑞丰软件定制合同", "厦门瑞丰软件有限公司", owners["evaluator"], True, False,
         *products["定制报表（张）"]),
        ("厦门瑞丰软件定制合同二期", "厦门瑞丰软件有限公司", owners["evaluator"], True, False,
         *products["接口开发包（套）"]),
        ("厦门海湾硬件网关合同（增补）", "厦门海湾食品有限公司", owners["evaluator"], True, False,
         *products["硬件网关"]),
        ("深圳市鹏城物联企业版订阅合同", "深圳市鹏城物联科技有限公司", owners["evaluator"], True, False,
         *products["企业版订阅（年）"]),
        ("深圳市鹏城物联企业版订阅合同（增补）", "深圳市鹏城物联科技有限公司", owners["evaluator"],
         False, False, *products["企业版订阅（两年）"]),
    ] + specs
    out, new = [], 0
    for name, company, owner, submit, reject, pid, price in specs:
        cid = customers[company][0]
        row = c.find("crm/contract/page", "name", name, name=name)
        if row is None:
            contact_id = next((v for (cc, _p), v in contacts.items() if cc == company), None)
            kid = c.post("crm/contract/create", {
                "name": name, "customerId": cid, "ownerUserId": owner,
                "orderDate": day(-260 + (zlib.crc32(name.encode()) % 250)),
                "startTime": day(-250 + (zlib.crc32(name.encode()) % 250)),
                "endTime": day(120 + (zlib.crc32(name.encode()) % 90)),
                "discountPercent": 0, "totalPrice": price * 2,
                "signContactId": contact_id, "signUserId": owner,
                "products": [{"productId": pid, "productPrice": price, "contractPrice": price, "count": 2}],
                "remark": ""})
            row = c.get("crm/contract/get", id=kid)
            new += 1
        if submit and row["auditStatus"] == 0:
            try:
                c.put("crm/contract/submit", id=row["id"])
                row = c.get("crm/contract/get", id=row["id"])
            except ApiError as e:
                print(f"contract submit {name} failed: {e}")
        out.append((row["id"], name, owner, reject))
    print(f"contracts: {len(out)} planned ({new} new this run)", flush=True)
    return out


def bpm_pending(client: Client, key_prefix: str) -> list:
    todo, page = [], 1
    while True:
        rows = client.page("bpm/task/todo-page", pageNo=page, pageSize=100)
        if not rows:
            return todo
        todo += [t for t in rows if (t.get("processInstance") or {}).get(
            "processDefinitionId", "").startswith(key_prefix + ":")]
        if len(rows) < 100:
            return todo
        page += 1


def approve_contracts(admin: Client, password: str, plan: list, keep_pending: int):
    reject_ids = {str(i) for i, _n, _o, rej in plan if rej}
    zhang = Client(admin.base, admin.tenant)
    zhang.login("zhangwei", password)
    tasks = sorted(bpm_pending(zhang, "crm-contract-audit"), key=lambda t: t["createTime"])
    work = tasks[: max(0, len(tasks) - keep_pending)]
    approved = rejected = failed = 0
    for t in work:
        try:  # the todo-page item carries no businessKey; read it from the instance itself
            bk = zhang.get("bpm/process-instance/get", id=t["processInstanceId"]).get("businessKey")
        except ApiError:
            bk = None
        try:
            if bk in reject_ids:
                zhang.put("bpm/task/reject", {"id": t["id"], "reason": "金额与报价单不符"})
                rejected += 1
            else:
                zhang.put("bpm/task/approve", {"id": t["id"], "reason": "同意"})
                approved += 1
        except ApiError as e:
            failed += 1
            print(f"contract approval {t['id']} failed: {e}")
    print(f"contracts: approved {approved}, rejected {rejected}, "
          f"left pending {len(tasks) - len(work)}, failed {failed}", flush=True)


def seed_receivables(admin: Client, owners: dict, password: str):
    approved = sorted((k for k in admin.pages("crm/contract/page") if k["auditStatus"] == 20),
                      key=lambda k: k["id"])
    plans_new = receipts_new = approvals = 0
    for i, k in enumerate(approved):
        if i % 47 == 13 and k["name"] != "厦门瑞丰软件定制合同":
            # a few approved contracts deliberately stay without a plan
            continue
        plans = admin.page("crm/receivable-plan/page", contractId=k["id"])
        if k["name"] == "厦门瑞丰软件定制合同" and not plans:
            # fixed offsets: period 2 is always one overdue open plan (its
            # receipt, if any, attaches to period 1 only)
            total = float(k["totalPrice"])
            for n, (frac, off) in enumerate([(0.5, -75), (0.3, -30), (0.2, 40)]):
                admin.post("crm/receivable-plan/create", {
                    "contractId": k["id"], "customerId": k["customerId"], "ownerUserId": k["ownerUserId"],
                    "returnTime": day(off, 9), "returnType": 1 + n % 3,
                    "price": round(total * frac, 2), "remindDays": 7})
            plans = admin.page("crm/receivable-plan/page", contractId=k["id"])
        elif not plans:  # v2 rows already carry their plans and are left as they are
            want = 2 + (i % 2)
            total = float(k["totalPrice"])
            cuts = [0.5, 0.3, 0.2] if want == 3 else [0.6, 0.4]
            for n, frac in enumerate(cuts):
                admin.post("crm/receivable-plan/create", {
                    "contractId": k["id"], "customerId": k["customerId"], "ownerUserId": k["ownerUserId"],
                    "returnTime": day(-160 + (i * 11) % 330 + n * 45, 9), "returnType": 1 + (i + n) % 3,
                    "price": round(total * frac, 2), "remindDays": 7 if i % 2 else 15})
                plans_new += 1
            plans = admin.page("crm/receivable-plan/page", contractId=k["id"])
        if i % 3 == 2 or not plans:
            continue
        first = sorted(plans, key=lambda p: p["period"])[0]
        if admin.page("crm/receivable/page", planId=first["id"]):
            continue
        price = first["price"]
        if i % 9 == 4:
            price = round(float(price) / 2, 2)  # partial: the application caps a receipt at its plan
        rid = admin.post("crm/receivable/create", {
            "ownerUserId": first["ownerUserId"], "customerId": first["customerId"], "contractId": k["id"],
            "planId": first["id"], "returnType": first.get("returnType") or 1,
            "price": price, "returnTime": day(-140 + (i * 9) % 260, 14)})
        receipts_new += 1
        if i % 12 != 11:  # a few stay drafts
            if admin.get("crm/receivable/get", id=rid)["auditStatus"] == 0:
                admin.try_put("crm/receivable/submit", id=rid)
    zhou = Client(admin.base, admin.tenant)
    zhou.login("zhouming", password)
    tasks = sorted(bpm_pending(zhou, "crm-receivable-audit"), key=lambda t: t["createTime"])
    for t in tasks[: max(0, len(tasks) - 14)]:  # keep ~14 in approval
        try:
            zhou.put("bpm/task/approve", {"id": t["id"], "reason": "确认到账"})
            approvals += 1
        except ApiError as e:
            print(f"receipt approval failed: {e}")
    print(f"receivables: {plans_new} plans, {receipts_new} receipts, {approvals} approvals this run",
          flush=True)


def seed_followups(admin: Client, customers: dict, owners: dict, password: str):
    """Follow-up records need WRITE on the business record, so owners write them."""
    pool = [(n, v) for n, v in customers.items() if v[1] == owners["evaluator"]][:120]
    pool += [(n, v) for n, v in customers.items()
             if v[1] in (owners["lina"], owners["wangqiang"])][:80]
    sessions, made, tag_i = {}, 0, 0
    for i, (company, (cid, owner)) in enumerate(pool):
        uname = next((u for u, uid in owners.items() if uid == owner and u != "admin"), None)
        if uname is None:
            continue
        if uname not in sessions:
            u = Client(admin.base, admin.tenant)
            u.login(uname, password)
            sessions[uname] = u
        u = sessions[uname]
        for n in range(1 + i % 3):
            tag_i += 1
            tag = f"V3F{tag_i:04d}"
            rows = u.page("crm/follow-up-record/page", bizType=2, bizId=cid)
            if any((r.get("content") or "").startswith(tag) for r in rows):
                continue
            u.post("crm/follow-up-record/create", {
                "bizType": 2, "bizId": cid, "type": 1 + (i + n) % 4,
                "content": f"{tag} 沟通交付计划{n}", "nextTime": day(5 + (i * 3) % 60, 16),
                # the page read streams both lists; a record created without them NPEs it
                "businessIds": [], "contactIds": []})
            made += 1
    print(f"follow-ups: {made} new this run", flush=True)


def seed_permissions(admin: Client, customers: dict, owners: dict, password: str):
    """The evaluator joins team records, added by each record's own owner."""
    ev = owners["evaluator"]
    sessions, made = {}, 0
    team = [(n, v) for n, v in customers.items()
            if v[1] in (owners["lina"], owners["wangqiang"])]
    # Position-capped (planned owners, dict in plan order): a rerun walks the
    # exact same 26 records and skips them via the already-sealed ev row. A
    # per-run success cap would keep granting to fresh records on every rerun.
    # crm/permission/list has no owner gate, so the owner session still sees
    # the row after seed_pool moves the record.
    for company, (cid, owner) in team[:26]:
        uname = next((u for u, uid in owners.items() if uid == owner and u != "admin"), None)
        if uname is None:
            continue
        if uname not in sessions:
            u = Client(admin.base, admin.tenant)
            u.login(uname, password)
            sessions[uname] = u
        u = sessions[uname]
        rows = u.get("crm/permission/list", bizType=2, bizId=cid) or []  # a bare list
        if any(r["userId"] == ev for r in rows):
            continue
        try:
            u.post("crm/permission/create", {"userId": ev, "bizType": 2, "bizId": cid, "level": 2})
            made += 1
        except ApiError as e:
            print(f"permission on {company}: {e}")
    print(f"collaborators: {made} evaluator rows added this run", flush=True)


def seed_pool(admin: Client, customers: dict, owners: dict, password: str):
    """A few v3 customers enter the public pool, moved by their own owner."""
    lina = Client(admin.base, admin.tenant)
    lina.login("lina", password)
    rows = [customers[n] for n in pool_target_names(owners)
            if n in customers and customers[n][1] == owners["lina"]]
    moved = 0
    live = {r["id"]: r for r in lina.pages("crm/customer/page", sceneType=1)}
    for cid, _owner in rows:
        if cid not in live or live[cid].get("ownerUserId") is None:
            continue
        try:
            lina.put("crm/customer/put-pool", id=cid)
            moved += 1
        except ApiError as e:
            print(f"put-pool {cid}: {e}")
    print(f"pool: {moved} customers moved this run", flush=True)


def seed_erp(admin: Client, owners: dict) -> None:
    """Purchases to back the stock, sale orders, shipments and a few returns."""
    account = admin.find("erp/account/page", "name", "招商银行基本户", name="招商银行基本户")["id"]
    products = {p["id"]: p for p in admin.pages("erp/product/page")}
    wh = [w["id"] for w in admin.page("erp/warehouse/page", pageSize=100)]
    suppliers = [s["id"] for s in admin.page("erp/supplier/page", pageSize=100)]
    ecustomers = [k["id"] for k in admin.page("erp/customer/page", pageSize=100)]
    pids = sorted(products)

    def stock_of(pid):
        return max((float(r["count"]) for r in admin.page("erp/stock/page", productId=pid)), default=0.0)

    def items_of(order, count_of=lambda it: it["count"]):
        return [{"orderItemId": it["id"], "warehouseId": wh[0], "productId": it["productId"],
                 "productUnitId": it["productUnitId"], "productPrice": it["productPrice"],
                 "count": count_of(it), "taxPercent": 13} for it in order["items"]]

    for i in range(8):
        tag = f"V3-P{i + 1:02d}"
        if admin.page("erp/purchase-order/page", remark=tag):
            continue
        lines = [{"productId": pid, "productUnitId": products[pid]["unitId"],
                  "productPrice": float(products[pid]["purchasePrice"]), "count": 200, "taxPercent": 13}
                 for pid in (pids[(i * 3 + n) % len(pids)] for n in range(3))]
        oid = admin.post("erp/purchase-order/create", {
            "supplierId": suppliers[i % len(suppliers)], "accountId": account,
            "orderTime": day(-120 + i * 4), "discountPercent": 0, "remark": tag, "items": lines})
        order = admin.get("erp/purchase-order/get", id=oid)
        if order["status"] == 10:
            admin.put("erp/purchase-order/update-status", id=oid, status=20)
            order = admin.get("erp/purchase-order/get", id=oid)
        iid = admin.post("erp/purchase-in/create", {
            "orderId": oid, "accountId": account, "inTime": day(-110 + i * 4),
            "discountPercent": 0, "otherPrice": 0, "remark": tag, "items": items_of(order)})
        if admin.get("erp/purchase-in/get", id=iid)["status"] == 10:
            admin.put("erp/purchase-in/update-status", id=iid, status=20)

    sale_users = [owners["zhangwei"], owners["lina"], owners["wangqiang"], owners["liuyang"], owners["chenjing"]]
    made = outs = rets = 0
    for i in range(130):
        tag = f"V3-S{i + 1:03d}"
        if admin.page("erp/sale-order/page", remark=tag):
            continue
        picked = [(pids[(i * 5 + n) % len(pids)], 4 + (i + n) % 6)
                  for n in range(1 + i % 3)]
        lines = [{"productId": pid, "productUnitId": products[pid]["unitId"],
                  "productPrice": float(products[pid]["salePrice"]), "count": cnt,
                  "taxPercent": 13} for pid, cnt in picked]
        oid = admin.post("erp/sale-order/create", {
            "customerId": ecustomers[i % len(ecustomers)], "accountId": account,
            "saleUserId": sale_users[i % 5], "orderTime": day(-85 + (i * 2) % 80),
            "discountPercent": 0, "remark": tag, "items": lines})
        made += 1
        if i % 4 == 3:
            continue  # stays unapproved
        admin.put("erp/sale-order/update-status", id=oid, status=20)
        if i % 10 not in (0, 1, 4, 6):
            continue
        order = admin.get("erp/sale-order/get", id=oid)
        if any(stock_of(it["productId"]) < float(it["count"]) for it in order["items"]):
            continue
        try:
            out = admin.post("erp/sale-out/create", {
                "orderId": oid, "accountId": account, "outTime": day(-30 + (i * 3) % 55),
                "discountPercent": 0, "otherPrice": 0, "remark": tag, "items": items_of(order)})
            admin.put("erp/sale-out/update-status", id=out, status=20)
            outs += 1
            if i % 20 == 0:
                ro = admin.post("erp/sale-return/create", {
                    "orderId": oid, "accountId": account, "returnTime": day(-10 + i % 30),
                    "discountPercent": 0, "otherPrice": 0, "remark": tag,
                    "items": items_of(order, lambda it: max(1, int(it["count"]) // 4))[:1]})
                admin.put("erp/sale-return/update-status", id=ro, status=20)
                rets += 1
        except ApiError as e:
            print(f"erp {tag} not shipped: {e}")
    print(f"erp: {made} sale orders, {outs} shipments, {rets} returns this run", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:48088")
    ap.add_argument("--tenant", default="1")
    args = ap.parse_args()
    c = Client(args.base, args.tenant)
    c.login(os.environ.get("YUDAO_ADMIN_USERNAME", "admin"),
            os.environ.get("YUDAO_ADMIN_PASSWORD", "admin123"))
    password = os.environ.get("YUDAO_SEED_PASSWORD", "Seed-local-2026")

    owners = {"admin": 1}
    for name in ["zhangwei", "lina", "wangqiang", "liuyang", "chenjing", "zhouming", "evaluator"]:
        owners[name] = c.find("system/user/page", "username", name, username=name)["id"]
    ensure_crm_admin(c, owners["admin"])

    t0 = time.time()
    products = seed_products(c, owners["zhangwei"])
    customers = seed_customers(c, owners)
    seed_clues(c, owners)
    contacts = seed_contacts(c, customers, owners)
    seed_businesses(c, customers, contacts, owners, products)
    plan = seed_contracts(c, customers, contacts, owners, products)
    time.sleep(2)  # let Flowable settle before paging the approver's todo list
    approve_contracts(c, password, plan, keep_pending=26)
    seed_receivables(c, owners, password)
    seed_followups(c, customers, owners, password)
    seed_permissions(c, customers, owners, password)
    seed_pool(c, customers, owners, password)
    seed_erp(c, owners)
    print(f"done in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    try:
        main()
    except ApiError as e:
        sys.exit(f"seed_v3 failed: {e}")
