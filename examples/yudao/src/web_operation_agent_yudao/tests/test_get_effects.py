"""No models or applications; the release scan over a small synthetic server source."""
import unittest
from unittest.mock import patch

from web_operation_agent_yudao.get_effects import fence_policy, scan

_BASE = "server/yudao-module-demo/src/main/java/demo/"
_SOURCES = {
    _BASE + "controller/admin/DemoController.java": '''
package demo.controller.admin;
@RestController
@RequestMapping("/demo/item")
public class DemoController {
    @Resource
    private DemoService demoService;
    @Resource
    private OtherApi otherApi;

    @GetMapping("/page")
    @PreAuthorize("@ss.hasPermission('demo:item:query')")
    public CommonResult<PageResult<Item>> getPage(@Valid DemoPageReqVO req) {
        // demoService.createItem(req) in a comment is not a call
        String text = "itemMapper.insert(x)";
        return success(demoService.getPage(req));
    }

    @GetMapping("/get")
    public CommonResult<Item> get(@RequestParam("id") Long id) {
        return success(demoService.getAndTouch(id));
    }

    @GetMapping("/groups")
    public CommonResult<List<Group>> groups() {
        return success(demoService.getGroups());
    }

    @GetMapping(value = "/{id}/remote")
    public CommonResult<Boolean> remote(@PathVariable("id") Long id) {
        return success(otherApi.syncItem(id));
    }

    @GetMapping("/names")
    public CommonResult<List<String>> names() {
        List<String> names = new ArrayList<>();
        names.add("a");
        return success(convertList(demoService.getPage(null), Item::getName));
    }

    @PostMapping("/create")
    public CommonResult<Long> create(@RequestBody Item item) {
        return success(demoService.createItem(item));
    }
}
''',
    _BASE + "controller/app/AppDemoController.java": '''
@RestController
@RequestMapping("/demo/item")
public class AppDemoController {
    @Resource
    private DemoService demoService;
    @GetMapping("/touch")
    public CommonResult<Item> touch(Long id) { return success(demoService.getAndTouch(id)); }
}
''',
    _BASE + "service/DemoService.java": '''
public interface DemoService {
    PageResult<Item> getPage(DemoPageReqVO req);
    Item getAndTouch(Long id);
    List<Group> getGroups();
    Long createItem(Item item);
}
''',
    _BASE + "service/DemoServiceImpl.java": '''
@Service
public class DemoServiceImpl implements DemoService {
    @Resource
    private ItemMapper itemMapper;
    @Resource
    private GroupMapper groupMapper;

    @Override
    public PageResult<Item> getPage(DemoPageReqVO req) {
        return itemMapper.selectPage(req);
    }

    @Override
    public Item getAndTouch(Long id) {
        Item item = itemMapper.selectById(id);
        touch(item);
        return item;
    }

    private void touch(Item item) {
        itemMapper.updateById(new Item().setId(item.getId()).setVisitTime(now()));
    }

    @Override
    public List<Group> getGroups() {
        List<Group> groups = groupMapper.selectList();
        if (groups.isEmpty()) {
            groups.forEach(groupMapper::insert);
        }
        return groups;
    }

    @Override
    public Long createItem(Item item) {
        itemMapper.insert(item);
        return item.getId();
    }
}
''',
}


class _Tree:
    def files(self):
        return sorted(_SOURCES) + ["ui/src/views/demo/index.vue"]

    def read_text(self, path):
        return _SOURCES[path]

    def surface_digest(self):
        return "0" * 64


class GetEffectsTests(unittest.TestCase):
    def test_inherited_fields_helpers_and_super_calls_are_followed(self):
        base = _BASE + "service/BaseService.java"
        code = '''public class BaseService {
            protected ItemMapper itemMapper;
            public Item getAndTouch(Long id) {
                itemMapper.updateById(id);
                return itemMapper.selectById(id);
            }
        }'''
        impl = _BASE + "service/DemoServiceImpl.java"
        for body in ("", '''public Item getAndTouch(Long id) {
                return super.getAndTouch(id);
            }''', '''public Item getAndTouch(Long id) {
                return itemMapper.updateById(id);
            }'''):
            sources = {base: code, impl: "public class DemoServiceImpl extends BaseService {\n" + body + "\n}"}
            with self.subTest(body=body), patch.dict(_SOURCES, sources):
                found = {h.path: h.reason for h in scan(_Tree())}
                self.assertIn("ItemMapper.updateById", found["/admin-api/demo/item/get"])

    def test_unresolved_or_cyclic_inheritance_is_not_claimed_read_only(self):
        impl = _BASE + "service/DemoServiceImpl.java"
        for parent in ("MissingBase", "DemoServiceImpl"):
            with self.subTest(parent=parent), patch.dict(_SOURCES, {impl:
                    "public class DemoServiceImpl extends " + parent + " {\n"
                    "public Item getAndTouch(Long id) { return null; }\n}"}):
                found = {h.path: h.reason for h in scan(_Tree())}
                self.assertIn("inheritance", found["/admin-api/demo/item/get"])

    def test_lists_only_admin_get_handlers_that_reach_a_write(self):
        found = {handler.path: handler.reason for handler in scan(_Tree())}
        self.assertEqual(set(found), {
            "/admin-api/demo/item/page", "/admin-api/demo/item/get", "/admin-api/demo/item/groups",
            "/admin-api/demo/item/{id}/remote", "/admin-api/demo/item/names"})
        self.assertEqual(found["/admin-api/demo/item/page"], "")
        self.assertEqual(found["/admin-api/demo/item/names"], "")
        self.assertIn("ItemMapper.updateById", found["/admin-api/demo/item/get"])
        self.assertIn("GroupMapper.insert", found["/admin-api/demo/item/groups"])
        self.assertIn("OtherApi.syncItem (unresolved)", found["/admin-api/demo/item/{id}/remote"])

    def test_policy_names_the_writing_handlers_for_the_method_fence(self):
        policy = fence_policy(_Tree(), "v1")
        self.assertEqual((policy["schema_version"], policy["mode"], policy["release"]),
                         (2, "method-fence", "v1"))
        self.assertEqual(policy["writing_gets"], [
            "/admin-api/demo/item/get", "/admin-api/demo/item/groups",
            "/admin-api/demo/item/{id}/remote"])
        self.assertEqual(policy["scan"]["get_handlers"], 5)
        self.assertEqual(set(policy["scan"]["reasons"]), set(policy["writing_gets"]))


if __name__ == "__main__":
    unittest.main()
