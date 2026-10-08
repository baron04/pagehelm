package cn.iocoder.yudao.server.woa;

import cn.iocoder.yudao.framework.common.pojo.CommonResult;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import java.util.LinkedHashMap;
import java.util.Map;

import static cn.iocoder.yudao.framework.common.exception.enums.GlobalErrorCodeConstants.BAD_REQUEST;

/** Opens, closes and reports the signed-in user's read-only window. */
@RestController
@RequestMapping("/admin-api/woa/page-guard")
public class WoaPageGuardController {

    private final WoaPageGuard guard;

    public WoaPageGuardController(WoaPageGuard guard) {
        this.guard = guard;
    }

    private static Map<String, Object> state(long expiresAt) {
        Map<String, Object> state = new LinkedHashMap<>();
        state.put("active", expiresAt > 0);
        state.put("expires_at", expiresAt);
        state.put("guard", "read_only_statements");
        return state;
    }

    @PostMapping("/begin")
    public CommonResult<Map<String, Object>> begin(@RequestBody Map<String, Object> body) {
        Object ttl = body.get("ttl_s");
        if (!(ttl instanceof Number) || ((Number) ttl).doubleValue() < 1
                || ((Number) ttl).doubleValue() > WoaPageGuard.MAX_TTL_SECONDS) {
            return CommonResult.error(BAD_REQUEST.getCode(), "invalid_page_guard_request");
        }
        return CommonResult.success(state(guard.open((long) Math.ceil(((Number) ttl).doubleValue()))));
    }

    @PostMapping("/end")
    public CommonResult<Map<String, Object>> end() {
        guard.close();
        return CommonResult.success(state(0));
    }

    @GetMapping("/status")
    public CommonResult<Map<String, Object>> status() {
        return CommonResult.success(state(guard.expiresAt()));
    }
}
