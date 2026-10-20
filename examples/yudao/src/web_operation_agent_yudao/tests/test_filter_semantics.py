"""No models or applications; the release scan over a small synthetic server source."""
import json
import unittest
from pathlib import Path

from web_operation_agent_yudao.filter_semantics import LABELS, SCHEMA, FilterSemantics, filter_policy

_BASE = "server/yudao-module-demo/src/main/java/demo/"
_SOURCES = {
    _BASE + "controller/admin/DemoController.java": '''
package demo.controller.admin;
@RestController
@RequestMapping("/demo/item")
public class DemoController {
    @Resource
    private DemoService demoService;

    @GetMapping("/page")
    @PreAuthorize("@ss.hasPermission('demo:item:query')")
    public CommonResult<PageResult<Item>> getPage(@Valid DemoPageReqVO req) {
        return success(demoService.getPage(req));
    }

    @GetMapping("/get")
    public CommonResult<Item> get(@RequestParam("id") Long id) {
        return success(demoService.get(id));
    }

    @GetMapping("/export-excel")
    public void export(@Valid DemoPageReqVO req, HttpServletResponse response) {
    }

    @GetMapping("/list-all")
    public CommonResult<List<Item>> listAll(@Valid DemoListReqVO req) {
        return success(demoService.list(req));
    }

    @GetMapping("/contact/page")
    public CommonResult<PageResult<Item>> contactPage(@Valid ContactLikePageReqVO req) {
        return success(demoService.getContactPage(req));
    }

    @GetMapping("/contact/page-by-customer")
    public CommonResult<PageResult<Item>> contactPageByCustomer(@Valid ContactLikePageReqVO req) {
        return success(demoService.getContactPageByCustomerId(req));
    }

    @GetMapping("/conflict/page")
    public CommonResult<PageResult<Item>> conflictPage(@Valid ConflictPageReqVO req) {
        return success(demoService.conflict(req));
    }

    @GetMapping("/stats/page")
    public CommonResult<PageResult<Item>> statsPage(@Valid StatsPageReqVO req) {
        return success(demoService.stats(req));
    }
}
''',
    _BASE + "controller/admin/vo/DemoPageReqVO.java": '''
package demo.controller.admin.vo;
@Schema(description = "分页 Request VO")
@Data
public class DemoPageReqVO extends PageParam {

    @Schema(description = "名称", example = "x")
    private String name;

    private Long categoryId;

    @DateTimeFormat(pattern = FORMAT_YEAR_MONTH_DAY_HOUR_MINUTE_SECOND)
    private LocalDateTime[] createTime;

    private Integer status;

    private Integer channelId;

    private static final Long SERIAL = 1L;
}
''',
    _BASE + "controller/admin/vo/DemoListReqVO.java": '''
package demo.controller.admin.vo;
public class DemoListReqVO {
    private String keyword;
}
''',
    _BASE + "controller/admin/vo/ContactLikePageReqVO.java": '''
package demo.controller.admin.vo;
public class ContactLikePageReqVO extends PageParam {
    private String name;
    private String mobile;
    private Long customerId;
}
''',
    _BASE + "controller/admin/vo/ConflictPageReqVO.java": '''
package demo.controller.admin.vo;
public class ConflictPageReqVO extends PageParam {
    private Integer code;
}
''',
    _BASE + "controller/admin/vo/StatsPageReqVO.java": '''
package demo.controller.admin.vo;
public class StatsPageReqVO extends PageParam {
    private List<Long> ids;
    private BigDecimal minPrice;
    private String noPrefix;
}
''',
    _BASE + "dal/mysql/DemoMapper.java": '''
package demo.dal.mysql;
@Mapper
public interface DemoMapper extends BaseMapperX<DemoItemDO> {

    default PageResult<DemoItemDO> selectPage(DemoPageReqVO reqVO) {
        return selectPage(reqVO, new LambdaQueryWrapperX<DemoItemDO>()
                .likeIfPresent(DemoItemDO::getName, reqVO.getName())
                .eqIfPresent(DemoItemDO::getCategoryId, reqVO.getCategoryId())
                .betweenIfPresent(DemoItemDO::getCreateTime, reqVO.getCreateTime())
                .eq(DemoItemDO::getStatus, reqVO.getStatus())
                .orderByDesc(DemoItemDO::getId));
    }

    default List<DemoItemDO> selectList(DemoPageReqVO reqVO) {
        return selectList(new LambdaQueryWrapperX<DemoItemDO>()
                .likeRightIfPresent(DemoItemDO::getNo, reqVO.getNo()));
    }
}
''',
    # A second mapper method binding the same VO: only what every querying method
    # binds alike stays a fact about the endpoint's read.
    _BASE + "dal/mysql/OtherDemoMapper.java": '''
package demo.dal.mysql;
@Mapper
public interface OtherDemoMapper extends BaseMapperX<DemoItemDO> {
    default List<DemoItemDO> selectByCategory(DemoPageReqVO reqVO) {
        return selectList(new LambdaQueryWrapperX<DemoItemDO>()
                .inIfPresent(DemoItemDO::getChannelId, reqVO.getChannelId())
                .likeIfPresent(DemoItemDO::getName, reqVO.getName()));
    }
}
''',
    # The deployed CRM contact shape: the page endpoint's method has no customerId
    # condition; another endpoint's method does. Neither endpoint may claim it.
    _BASE + "dal/mysql/ContactLikeMapper.java": '''
package demo.dal.mysql;
@Mapper
public interface ContactLikeMapper extends BaseMapperX<ContactDO> {
    default PageResult<ContactDO> selectPage(ContactLikePageReqVO reqVO, Long userId) {
        return selectPage(reqVO, new LambdaQueryWrapperX<ContactDO>()
                .likeIfPresent(ContactDO::getName, reqVO.getName())
                .eqIfPresent(ContactDO::getMobile, reqVO.getMobile())
                .orderByDesc(ContactDO::getId));
    }

    default PageResult<ContactDO> selectPageByCustomerId(ContactLikePageReqVO reqVO) {
        return selectPage(reqVO, new LambdaQueryWrapperX<ContactDO>()
                .eq(ContactDO::getCustomerId, reqVO.getCustomerId())
                .likeIfPresent(ContactDO::getName, reqVO.getName())
                .eqIfPresent(ContactDO::getMobile, reqVO.getMobile())
                .orderByDesc(ContactDO::getId));
    }
}
''',
    _BASE + "dal/mysql/ConflictMapper.java": '''
package demo.dal.mysql;
@Mapper
public interface ConflictMapper extends BaseMapperX<ConflictDO> {
    default PageResult<ConflictDO> selectPage(ConflictPageReqVO reqVO) {
        return selectPage(reqVO, new LambdaQueryWrapperX<ConflictDO>()
                .eqIfPresent(ConflictDO::getCode, reqVO.getCode()));
    }

    default PageResult<ConflictDO> selectPageAlt(ConflictPageReqVO reqVO) {
        return selectPage(reqVO, new LambdaQueryWrapperX<ConflictDO>()
                .inIfPresent(ConflictDO::getCode, reqVO.getCode()));
    }
}
''',
    _BASE + "dal/mysql/StatsMapper.java": '''
package demo.dal.mysql;
@Mapper
public interface StatsMapper extends BaseMapperX<StatsDO> {
    default PageResult<StatsDO> selectPage(StatsPageReqVO reqVO) {
        return selectPage(reqVO, new LambdaQueryWrapperX<StatsDO>()
                .inIfPresent(StatsDO::getId, reqVO.getIds())
                .geIfPresent(StatsDO::getPrice, reqVO.getMinPrice())
                .likeRightIfPresent(StatsDO::getNo, reqVO.getNoPrefix())
                .orderByDesc(StatsDO::getId));
    }
}
''',
}

#: The deployed release's own three files, verbatim, as the parsing contract.
_ERP = "server/yudao-module-erp/src/main/java/cn/iocoder/yudao/module/erp/"
_REAL = {
    _ERP + "controller/admin/product/ErpProductController.java": '''
package cn.iocoder.yudao.module.erp.controller.admin.product;

@Tag(name = "管理后台 - ERP 产品")
@RestController
@RequestMapping("/erp/product")
@Validated
public class ErpProductController {

    @Resource
    private ErpProductService productService;

    @GetMapping("/page")
    @Operation(summary = "获得产品分页")
    @PreAuthorize("@ss.hasPermission('erp:product:query')")
    public CommonResult<PageResult<ErpProductRespVO>> getProductPage(@Valid ErpProductPageReqVO pageReqVO) {
        return success(BeanUtils.toBean(productService.getProductPage(pageReqVO), ErpProductRespVO.class));
    }
}
''',
    _ERP + "controller/admin/product/vo/product/ErpProductPageReqVO.java": '''
package cn.iocoder.yudao.module.erp.controller.admin.product.vo.product;

@Schema(description = "管理后台 - ERP 产品分页 Request VO")
@Data
@EqualsAndHashCode(callSuper = true)
@ToString(callSuper = true)
public class ErpProductPageReqVO extends PageParam {

    @Schema(description = "产品名称", example = "李四")
    private String name;

    @Schema(description = "产品分类编号", example = "11161")
    private Long categoryId;

    @Schema(description = "创建时间")
    @DateTimeFormat(pattern = FORMAT_YEAR_MONTH_DAY_HOUR_MINUTE_SECOND)
    private LocalDateTime[] createTime;

}
''',
    _ERP + "dal/mysql/product/ErpProductMapper.java": '''
package cn.iocoder.yudao.module.erp.dal.mysql.product;

@Mapper
public interface ErpProductMapper extends BaseMapperX<ErpProductDO> {

    default PageResult<ErpProductDO> selectPage(ErpProductPageReqVO reqVO) {
        return selectPage(reqVO, new LambdaQueryWrapperX<ErpProductDO>()
                .likeIfPresent(ErpProductDO::getName, reqVO.getName())
                .eqIfPresent(ErpProductDO::getCategoryId, reqVO.getCategoryId())
                .betweenIfPresent(ErpProductDO::getCreateTime, reqVO.getCreateTime())
                .orderByDesc(ErpProductDO::getId));
    }

    default Long selectCountByCategoryId(Long categoryId) {
        return selectCount(ErpProductDO::getCategoryId, categoryId);
    }

    default Long selectCountByUnitId(Long unitId) {
        return selectCount(ErpProductDO::getUnitId, unitId);
    }

    default List<ErpProductDO> selectListByStatus(Integer status) {
        return selectList(ErpProductDO::getStatus, status);
    }

}
''',
}


class _Tree:
    def __init__(self, sources):
        self._sources = sources

    def files(self):
        return sorted(self._sources)

    def read_text(self, path):
        return self._sources[path]

    def surface_digest(self):
        return "digest"


def _doc(**over):
    document = {"schema_version": SCHEMA, "release": "v-test", "source_digest": "digest",
                "labels": LABELS,
                "apis": {"/admin-api/x/page": {"req_vo": "XPageReqVO",
                                               "params": {"name": "like"}, "unknown": []}}}
    document.update(over)
    return document


class CompileTests(unittest.TestCase):
    def test_only_what_every_querying_method_binds_alike_is_resolved(self):
        apis = filter_policy(_Tree(_SOURCES), "v-test")["apis"]
        # selectPage binds four parameters; selectByCategory binds channelId and name.
        # Only name is a fact about every read this VO can serve. selectList binds no
        # VO field and makes no claim, so it does not void the intersection.
        self.assertEqual(apis["/admin-api/demo/item/page"], {
            "req_vo": "DemoPageReqVO",
            "params": {"name": "like"},
            "unknown": ["categoryId", "channelId", "createTime", "status"]})

    def test_a_branch_condition_of_another_endpoint_stays_unknown(self):
        apis = filter_policy(_Tree(_SOURCES), "v-test")["apis"]
        for endpoint in ("/admin-api/demo/item/contact/page",
                         "/admin-api/demo/item/contact/page-by-customer"):
            with self.subTest(endpoint=endpoint):
                self.assertEqual(apis[endpoint], {
                    "req_vo": "ContactLikePageReqVO",
                    "params": {"name": "like", "mobile": "eq"},
                    "equalities": {"mobile": "mobile"},
                    "unknown": ["customerId"]})

    def test_a_parameter_two_methods_bind_differently_stays_unknown(self):
        apis = filter_policy(_Tree(_SOURCES), "v-test")["apis"]
        self.assertEqual(apis["/admin-api/demo/item/conflict/page"], {
            "req_vo": "ConflictPageReqVO", "params": {}, "unknown": ["code"]})

    def test_membership_comparison_and_prefix_conditions_keep_their_own_codes(self):
        apis = filter_policy(_Tree(_SOURCES), "v-test")["apis"]
        self.assertEqual(apis["/admin-api/demo/item/stats/page"], {
            "req_vo": "StatsPageReqVO",
            "params": {"ids": "in", "minPrice": "ge", "noPrefix": "likeRight"},
            "unknown": []})
        found = FilterSemantics.load(
            {"schema_version": SCHEMA, "release": "v-test", "source_digest": "digest",
             "labels": LABELS, "apis": apis}, release="v-test")
        self.assertEqual(found.params_for("/admin-api/demo/item/stats/page"),
                         {"ids": "属于", "minPrice": "大于等于", "noPrefix": "前缀匹配"})

    def test_handlers_without_a_request_vo_and_exports_are_not_compiled(self):
        apis = filter_policy(_Tree(_SOURCES), "v-test")["apis"]
        self.assertNotIn("/admin-api/demo/item/get", apis)
        self.assertNotIn("/admin-api/demo/item/export-excel", apis)

    def test_a_vo_no_mapper_resolves_is_recorded_as_unknown(self):
        apis = filter_policy(_Tree(_SOURCES), "v-test")["apis"]
        self.assertEqual(apis["/admin-api/demo/item/list-all"],
                         {"req_vo": "DemoListReqVO", "params": {}, "unknown": ["keyword"]})

    def test_the_deployed_release_own_files_compile_to_the_documented_example(self):
        apis = filter_policy(_Tree(_REAL), "v2026.09")["apis"]
        self.assertEqual(apis["/admin-api/erp/product/page"], {
            "req_vo": "ErpProductPageReqVO",
            "params": {"name": "like", "categoryId": "eq", "createTime": "between"},
            "equalities": {"categoryId": "categoryId"},
            "unknown": []})

    def test_only_direct_same_field_and_untransformed_values_become_executable_equalities(self):
        for changed in (".eqIfPresent(ErpProductDO::getUnitId, reqVO.getCategoryId())",
                        ".eqIfPresent(ErpProductDO::getCategoryId, reqVO.getCategoryId() + 1)",
                        '.eqIfPresent("category_id", reqVO.getCategoryId())'):
            sources = dict(_REAL)
            path = next(p for p in sources if p.endswith("Mapper.java"))
            sources[path] = sources[path].replace(
                ".eqIfPresent(ErpProductDO::getCategoryId, reqVO.getCategoryId())", changed)
            with self.subTest(changed=changed):
                entry = filter_policy(_Tree(sources), "v-test")["apis"]["/admin-api/erp/product/page"]
                self.assertEqual(entry["params"]["categoryId"], "eq")
                self.assertNotIn("equalities", entry)

    def test_compiled_equalities_load_but_old_annotation_only_policies_prove_none(self):
        compiled = filter_policy(_Tree(_REAL), "v-test")
        found = FilterSemantics.load(compiled, release="v-test")
        self.assertEqual(found.equalities_for("/admin-api/erp/product/page"), {"categoryId": "categoryId"})
        compiled["apis"]["/admin-api/erp/product/page"].pop("equalities")
        self.assertEqual(FilterSemantics.load(compiled, release="v-test").equalities_for(
            "/admin-api/erp/product/page"), {})
        for bindings in ({"categoryId": "unitId"}, {"name": "name"}):
            compiled["apis"]["/admin-api/erp/product/page"]["equalities"] = bindings
            with self.assertRaises(ValueError):
                FilterSemantics.load(compiled, release="v-test")

    def test_the_policy_binds_the_release_and_the_source_it_was_read_from(self):
        policy = filter_policy(_Tree(_SOURCES), "v-test")
        self.assertEqual((policy["schema_version"], policy["release"], policy["source_digest"]),
                         (SCHEMA, "v-test", "digest"))
        self.assertEqual(policy["labels"], LABELS)


class LoadTests(unittest.TestCase):
    def test_loaded_labels_answer_by_endpoint_and_parameter(self):
        found = FilterSemantics.load(_doc(), release="v-test", source_digest="digest")
        self.assertEqual(found.params_for("/admin-api/x/page"), {"name": "包含匹配"})
        self.assertEqual(found.params_for("/admin-api/other/page"), {})
        self.assertEqual(found.condition_line("/admin-api/x/page", ["name", "status"]), "name 包含匹配")

    def test_a_policy_of_another_release_or_source_is_refused(self):
        for document, kwargs in (
                (_doc(release="v-other"), {"release": "v-test"}),
                (_doc(source_digest="other"), {"release": "v-test", "source_digest": "digest"}),
                (_doc(schema_version=2), {"release": "v-test"}),
                (_doc(labels={**LABELS, "eq": "等于匹配"}), {"release": "v-test"}),
                (_doc(apis={"/admin-api/x/page": {"params": {"name": "sounds-like"}}}),
                 {"release": "v-test"}),
                (_doc(apis={"/admin-api/x/page": {"params": "name"}}), {"release": "v-test"})):
            with self.subTest(document=document):
                with self.assertRaises(ValueError):
                    FilterSemantics.load(document, **kwargs)


class DeploymentWiringTests(unittest.TestCase):
    """Startup binds the filter policy to the same source the method fence was read from."""

    FENCE = {"schema_version": 2, "release": "v-test", "mode": "method-fence",
             "writing_gets": [], "scan": {"source_digest": "digest"}}
    PAGES = {"schema_version": 1, "release": "v-test", "review": "v-test",
             "handlers": [{"kind": "navigation", "route": "/contacts", "controls": [],
                           "evidence": "v-test", "sha256": "a" * 64}]}

    def _validate(self, root, filters_document):
        from types import SimpleNamespace as NS
        from web_operation_agent_yudao.deployment_validation import validate_native_deployment
        fence, pages = root / "fence.json", root / "pages.json"
        fence.write_text(json.dumps(self.FENCE))
        pages.write_text(json.dumps(self.PAGES))
        filters = root / "filters.json"
        filters.write_text(json.dumps(filters_document))
        return validate_native_deployment(NS(
            native_read_policy=fence, native_page_policy=pages, native_money_policy=None,
            native_filter_semantics=filters, source_release="v-test", ui_source=None))

    def test_a_filter_policy_of_another_source_stops_startup(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            for document in (_doc(source_digest="other"), _doc(release="v-other")):
                with self.subTest(document=document):
                    with self.assertRaises(ValueError):
                        self._validate(Path(directory), document)

    def test_a_matching_filter_policy_loads_with_the_validated_deployment(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            validated = self._validate(Path(directory), _doc())
            self.assertEqual(validated.filter_semantics.params_for("/admin-api/x/page"),
                             {"name": "包含匹配"})

    def test_the_filter_policy_stays_optional(self):
        import tempfile
        from types import SimpleNamespace as NS
        from web_operation_agent_yudao.deployment_validation import validate_native_deployment
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "fence.json").write_text(json.dumps(self.FENCE))
            (root / "pages.json").write_text(json.dumps(self.PAGES))
            validated = validate_native_deployment(NS(
                native_read_policy=root / "fence.json", native_page_policy=root / "pages.json",
                native_money_policy=None, source_release="v-test", ui_source=None))
            self.assertIsNone(validated.filter_semantics)


class DeployedPolicyTests(unittest.TestCase):
    """The committed policy is the one the deployment ships; it stays bound to the fence."""

    POLICIES = Path(__file__).resolve().parents[3] / "deploy" / "cloud" / "native-policy"

    def test_the_committed_policy_matches_the_method_fence_source(self):
        fence = json.loads((self.POLICIES / "fence.json").read_text())
        document = json.loads((self.POLICIES / "filters.json").read_text())
        found = FilterSemantics.load(document, release=fence["release"],
                                     source_digest=fence["scan"]["source_digest"])
        self.assertEqual(found.params_for("/admin-api/erp/product/page"),
                         {"name": "包含匹配", "categoryId": "等于", "createTime": "区间"})
        # What only a branch of another endpoint binds, or the service layer decides,
        # stays unannotated.
        self.assertNotIn("customerId", found.params_for("/admin-api/crm/contact/page"))
        self.assertNotIn("sceneType", found.params_for("/admin-api/crm/customer/page"))


if __name__ == "__main__":
    unittest.main()
