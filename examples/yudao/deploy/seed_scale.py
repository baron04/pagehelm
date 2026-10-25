"""Add synthetic, paginated acceptance data to an isolated local Yudao instance.

Uses application APIs and exact deterministic keys, retaining existing records. A failed
mutation stops the run: inspect its application state before running again. No workflow,
payment, mail, role grant or external notification is submitted. Credentials are read from
a named entry of the private accounts file, never printed. The required backup is made by
the operator first.

By default each main entity gains 120 named records with varied dates, statuses, amounts
and linked lines: system users, posts, departments and notices; HRM employees; CRM
customers, contacts, leads, products, opportunities and draft contracts; ERP basics,
purchase and sale orders and approved purchase receipts, which create the application's
own stock records; WMS item specs; MES customers and suppliers; PMS projects. New users get
random passwords. The report is built from the full lists read back afterwards.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import json
from pathlib import Path
import secrets
import sys
from urllib.parse import urlsplit

from seed import Client

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "examples/yudao/src")]
from web_operation_agent_yudao.accounts import add_account_arguments, load_account  # noqa: E402

VERSION = "local-scale-v1"
PREFIX = "合成规模"
ANCHOR = datetime(2026, 10, 1, 10, tzinfo=timezone(timedelta(hours=8)))
CITIES = ("杭州", "苏州", "深圳", "成都", "武汉", "青岛")
GOODS = ("工业网关", "温湿度传感器", "伺服驱动器", "标签打印机", "包装纸箱", "导轨电源")


def day(offset):
    return int((ANCHOR + timedelta(days=offset)).timestamp() * 1000)


def name(kind, i):
    return f"{PREFIX}·{CITIES[i % len(CITIES)]}{kind}{i + 1:03d}"


class Scale:
    def __init__(self, client, size):
        self.c, self.size, self.cache, self.created = client, size, {}, {}

    def rows(self, path):
        # Do not silently cap at the first 100: resuming a large fixture must find its keys.
        if path not in self.cache:
            rows, page = [], 1
            while True:
                data = self.c.get(path, pageNo=page, pageSize=100)
                batch = data["list"] if isinstance(data, dict) else data
                rows.extend(batch)
                if not isinstance(data, dict) or len(rows) >= data["total"]:
                    break
                if not batch or page >= 100:
                    raise RuntimeError(f"incomplete fixture index: {path}")
                page += 1
            self.cache[path] = rows
        return self.cache[path]

    def ensure(self, route, body, *, key="name", listing="page"):
        path = f"{route}/{listing}"
        found = next((row for row in self.rows(path) if row.get(key) == body[key]), None)
        if found:
            return found["id"], False
        ident = self.c.post(f"{route}/create", body)
        self.cache[path].append({**body, "id": ident})
        self.created[route] = self.created.get(route, 0) + 1
        return ident, True

    def report(self, module):
        print(json.dumps({"module": module, "created": self.created}, ensure_ascii=False), flush=True)

    def organisation(self):
        root = next(d["id"] for d in self.rows("system/dept/list") if d["parentId"] == 0)
        depts, posts, users = [], [], []
        for i in range(12):
            dep, _ = self.ensure("system/dept", {"name": name("事业部", i), "parentId": root,
                "sort": 100 + i, "status": 0}, listing="list")
            post, _ = self.ensure("system/post", {"name": name("岗位", i), "code": f"scale_post_{i}",
                "sort": 100 + i, "status": int(i % 5 == 4)})
            depts.append(dep); posts.append(post)
        for i in range(self.size):
            username, nickname = f"scale{i + 1:03d}", name("员工", i)
            uid, _ = self.ensure("system/user", {"username": username, "nickname": nickname,
                "deptId": depts[i % 12], "postIds": [posts[i % 4]], "password": secrets.token_hex(8),
                "sex": 1 + i % 2, "email": f"{username}@example.invalid",
                "mobile": f"139900{i:05d}", "remark": VERSION}, key="username")
            # New fixture users have random unknown passwords and no assigned roles.
            self.ensure("hrm/employee", {"name": nickname, "userId": uid, "deptId": depts[i % 12],
                "jobNumber": f"SCALE{i + 1:04d}", "mobile": f"139900{i:05d}", "sex": 1 + i % 2,
                "entryStatus": 1, "status": 1, "type": 1, "entryTime": day(-600 + i * 3),
                "probation": 0, "postName": name("岗位", i % 12), "workCity": CITIES[i % 6],
                "email": f"{username}@example.invalid"}, key="jobNumber")
            users.append(uid)
        for i in range(self.size):
            self.ensure("system/notice", {"title": name("验收通知", i), "type": 1 + i % 2,
                "content": f"<p>{VERSION} 合成公告，{CITIES[i % 6]}分部第 {i + 1} 次记录，用于列表、筛选和详情验收。</p>",
                "status": int(i % 4 == 3)}, key="title")
        self.report("system/hrm")
        return users

    def crm(self, actor):
        cat, _ = self.ensure("crm/product-category", {"name": PREFIX + "产品", "parentId": 0}, listing="list")
        group, _ = self.ensure("crm/business-status", {"name": PREFIX + "销售流程", "deptIds": [],
            "statuses": [{"name": text, "percent": percent} for text, percent in
                         (("需求收集", 10), ("方案沟通", 40), ("商务谈判", 80))]})
        products = []
        for i in range(self.size):
            price = round(89.5 + i * 13.75, 2)
            pid, _ = self.ensure("crm/product", {"name": name(GOODS[i % 6], i), "no": f"SCALE-CP-{i + 1:04d}",
                "unit": 1, "price": price, "status": int(i % 7 != 6), "categoryId": cat,
                "ownerUserId": actor}, key="no")
            products.append((pid, price))
        for i in range(self.size):
            customer, _ = self.ensure("crm/customer", {"name": name("科技有限公司", i), "ownerUserId": actor,
                "industryId": 1 + i % 8, "level": 1 + i % 3, "source": 1 + i % 6,
                "mobile": f"138900{i:05d}", "detailAddress": f"{CITIES[i % 6]}市合成产业园 {i + 1} 号",
                "contactNextTime": day(-20 + i % 60), "remark": VERSION})
            contacts = []
            for j in range(2):
                contact, _ = self.ensure("crm/contact", {"name": name("采购经理" if j else "负责人", i),
                    "customerId": customer, "ownerUserId": actor, "mobile": f"137900{i * 2 + j:05d}",
                    "master": j == 0, "post": "采购经理" if j else "总经理", "sex": 1 + (i + j) % 2})
                contacts.append(contact)
            self.ensure("crm/clue", {"name": name("展会咨询", i), "ownerUserId": actor,
                "mobile": f"136900{i:05d}", "source": 1 + i % 6, "level": 1 + i % 3,
                "industryId": 1 + i % 8, "contactNextTime": day(i % 45), "remark": VERSION})
            indices = [(i + k) % self.size for k in range(1 + i % 3)]
            indices = [j if j % 7 != 6 else j - 1 for j in indices]
            lines = [{"productId": products[j][0], "productPrice": products[j][1],
                "businessPrice": products[j][1], "count": 1 + (i + k) % 5}
                for k, j in enumerate(dict.fromkeys(indices))]
            business, _ = self.ensure("crm/business", {"name": name("设备更新项目", i), "customerId": customer,
                "ownerUserId": actor, "statusTypeId": group, "dealTime": day(-90 + i * 2),
                "discountPercent": 0, "contactId": contacts[0], "products": lines})
            contract_lines = [{**{k: v for k, v in line.items() if k != "businessPrice"},
                               "contractPrice": line["businessPrice"]} for line in lines]
            self.ensure("crm/contract", {"name": name("设备采购合同", i), "customerId": customer,
                "businessId": business, "ownerUserId": actor, "orderDate": day(-180 + i),
                "startTime": day(-160 + i), "endTime": day(180 + i), "discountPercent": 0,
                "totalPrice": round(sum(line["contractPrice"] * line["count"] for line in contract_lines), 2),
                "signContactId": contacts[0], "signUserId": actor, "products": contract_lines})
        self.report("crm")

    def erp(self, actor):
        unit, _ = self.ensure("erp/product-unit", {"name": PREFIX + "件", "status": 0})
        categories = [self.ensure("erp/product-category", {"name": name("物料分类", i), "parentId": 0,
            "code": f"SCALE-EC-{i}", "sort": i, "status": 0}, listing="list")[0] for i in range(6)]
        warehouses = [self.ensure("erp/warehouse", {"name": name("分仓", i), "address": CITIES[i],
            "sort": 100 + i, "status": 0, "principal": "合成仓管员"})[0] for i in range(6)]
        account, _ = self.ensure("erp/account", {"name": PREFIX + "结算账户", "no": "SYNTHETIC-NONBANK",
            "status": 0, "sort": 100})
        products, suppliers, customers = [], [], []
        for i in range(self.size):
            pid, _ = self.ensure("erp/product", {"name": name(GOODS[i % 6], i), "barCode": f"SCALE-EP-{i + 1:04d}",
                "categoryId": categories[i % 6], "unitId": unit, "status": int(i % 9 == 8),
                "standard": f"规格 {i % 12 + 1} / Model-{i + 1:04d}", "purchasePrice": 8.5 + i * 2,
                "salePrice": 13.75 + i * 3, "minPrice": 10 + i * 2}, key="barCode")
            products.append(pid)
            for route, kind, target in (("erp/supplier", "材料供应商", suppliers), ("erp/customer", "渠道客户", customers)):
                ident, _ = self.ensure(route, {"name": name(kind, i), "contact": name("联系人", i),
                    "mobile": f"135900{i:05d}", "status": int(i % 8 == 7), "sort": i, "taxPercent": 13})
                target.append(ident)
        for i in range(self.size):
            # Active products/partners only, with 1–3 lines and varied dates and quantities.
            indices = [(i * 3 + k) % self.size for k in range(1 + i % 3)]
            indices = [j if j % 9 != 8 else j - 1 for j in indices]
            items = [{"productId": products[j], "productUnitId": unit, "productPrice": 8.5 + j * 2,
                      "count": 20 + (i + k) % 40, "taxPercent": 13} for k, j in enumerate(dict.fromkeys(indices))]
            partner = i if i % 8 != 7 else i - 1
            purchase_tag = f"{VERSION}-P{i + 1:04d}"
            oid, _ = self.ensure("erp/purchase-order", {"supplierId": suppliers[partner], "accountId": account,
                "orderTime": day(-180 + i), "discountPercent": 0, "remark": purchase_tag, "items": items}, key="remark")
            order = self.c.get("erp/purchase-order/get", id=oid)
            if i % 5 != 4:
                if order["status"] == 10:
                    self.c.put("erp/purchase-order/update-status", id=oid, status=20)
                receipt_tag = f"{VERSION}-IN{i + 1:04d}"
                iid, _ = self.ensure("erp/purchase-in", {"orderId": oid, "accountId": account,
                    "inTime": day(-175 + i), "discountPercent": 0, "otherPrice": 0, "remark": receipt_tag,
                    "items": [{"orderItemId": line["id"], "warehouseId": warehouses[i % 6],
                        **{key: line[key] for key in ("productId", "productUnitId", "productPrice", "count", "taxPercent")}}
                        for line in order["items"]]}, key="remark")
                if self.c.get("erp/purchase-in/get", id=iid)["status"] == 10:
                    self.c.put("erp/purchase-in/update-status", id=iid, status=20)
            self.ensure("erp/sale-order", {"customerId": customers[partner], "accountId": account,
                "saleUserId": actor, "orderTime": day(-90 + i), "discountPercent": 0,
                "remark": f"{VERSION}-S{i + 1:04d}", "items": [{**line, "productPrice": round(line["productPrice"] * 1.5, 2),
                                                              "count": 1 + i % 5} for line in items]}, key="remark")
        self.report("erp")

    def other_modules(self, actor):
        wcat, _ = self.ensure("wms/item-category", {"name": PREFIX + "仓储商品", "parentId": 0,
            "code": "SCALE-WC", "sort": 100, "status": 0}, listing="list")
        for i in range(self.size):
            self.ensure("wms/item", {"code": f"SCALE-WI-{i + 1:04d}", "name": name(GOODS[i % 6], i),
                "categoryId": wcat, "unit": "件", "remark": VERSION,
                "skus": [{"name": f"{shade} / {i % 12 + 1} 型", "code": f"SCALE-WS-{i:04d}-{j}",
                    "barCode": f"SCALE-WB-{i:04d}-{j}", "costPrice": 50 + i * 2,
                    "sellingPrice": 80.5 + i * 3, "grossWeight": 1 + i % 8}
                    for j, shade in enumerate(("标准版", "增强版")[:1 + i % 2])]}, key="code")
            for route, kind in (("mes/md-client", "制造客户"), ("mes/md-vendor", "制造供应商")):
                self.ensure(route, {"code": f"SCALE-{route.rsplit('-', 1)[-1]}-{i + 1:04d}",
                    "name": name(kind, i), "type": 1 + i % 2, "status": int(i % 8 == 7),
                    "address": f"{CITIES[i % 6]}合成工业园 {i + 1} 号", "remark": VERSION}, key="code")
            self.ensure("pms/pm/project", {"name": name("数字化项目", i), "type": 1 + i % 2,
                "level": 1 + i % 5, "description": VERSION, "openStatus": i % 3 != 2,
                "icon": "ep:folder", "startTime": day(-120 + i), "endTime": day(120 + i),
                "memberUserIds": [actor]})
        self.report("wms/mes/pms")

    def summary(self):
        return {"fixture": VERSION, "size_per_main_entity": self.size, "created": self.created,
            "totals": {path: len(self.rows(path)) for path in sorted(self.cache)}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://localhost:48163")
    add_account_arguments(parser)
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--size", type=int, default=120)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    url = urlsplit(args.base)
    if (not args.apply or url.scheme != "http" or url.hostname not in {"localhost", "127.0.0.1"}
            or url.username or url.password or url.path not in {"", "/"} or url.query or url.fragment
            or not 1 <= args.size <= 500):
        parser.error("requires --apply, a loopback HTTP application and --size between 1 and 500")
    with gzip.open(args.backup, "rb") as backup:
        if not backup.read(1024).startswith(b"-- MySQL dump"):
            parser.error("requires an existing MySQL dump backup")
    account = load_account(args.account, args.accounts)
    client = Client(args.base, str(account.get("tenant", "1")))
    client.login(account["username"], account["password"])
    actor = client.get("system/auth/get-permission-info")["user"]["id"]
    fixture = Scale(client, args.size)
    fixture.organisation()
    fixture.crm(actor)
    fixture.erp(actor)
    fixture.other_modules(actor)
    # Read all pages again: cached planned rows are not evidence of persisted records.
    paths = tuple(fixture.cache)
    fixture.cache.clear()
    for path in paths:
        fixture.rows(path)
    summary = fixture.summary()
    summary["backup_sha256"] = hashlib.sha256(args.backup.read_bytes()).hexdigest()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(summary, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
