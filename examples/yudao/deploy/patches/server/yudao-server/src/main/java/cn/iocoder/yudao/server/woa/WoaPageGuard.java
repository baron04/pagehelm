package cn.iocoder.yudao.server.woa;

import cn.iocoder.yudao.framework.security.core.LoginUser;
import cn.iocoder.yudao.framework.security.core.util.SecurityFrameworkUtils;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.stereotype.Component;

import java.time.Duration;
import java.time.Instant;

/**
 * The read-only window an embedded assistant opens around its page actions.
 *
 * While a signed-in user's window is open, a request from the page the assistant is working
 * is refused any insert, update or delete, whatever handler or HTTP method it came through.
 * Nothing else that user does is held. The window is kept in Redis under the tenant and
 * user, so every server instance sees it, and it closes by itself when its time runs out.
 */
@Component
public class WoaPageGuard {

    /** The longest a window may be asked to stay open. */
    public static final long MAX_TTL_SECONDS = 300;

    private static final ThreadLocal<Boolean> READ_ONLY = new ThreadLocal<>();

    private final StringRedisTemplate redis;

    public WoaPageGuard(StringRedisTemplate redis) {
        this.redis = redis;
    }

    private static String key(LoginUser user) {
        return "woa:page_guard:" + user.getTenantId() + ":" + user.getUserType() + ":" + user.getId();
    }

    /** Opens or extends the signed-in user's window; returns when it closes, in epoch seconds. */
    public long open(long ttlSeconds) {
        LoginUser user = SecurityFrameworkUtils.getLoginUser();
        if (user == null) {
            throw new IllegalStateException("no signed-in user");
        }
        long expiresAt = Instant.now().getEpochSecond() + ttlSeconds;
        redis.opsForValue().set(key(user), Long.toString(expiresAt), Duration.ofSeconds(ttlSeconds));
        return expiresAt;
    }

    public void close() {
        LoginUser user = SecurityFrameworkUtils.getLoginUser();
        if (user != null) {
            redis.delete(key(user));
        }
    }

    /** When the signed-in user's window closes, in epoch seconds; 0 when none is open. */
    public long expiresAt() {
        LoginUser user = SecurityFrameworkUtils.getLoginUser();
        if (user == null) {
            return 0;
        }
        String value = redis.opsForValue().get(key(user));
        return value == null ? 0 : Long.parseLong(value);
    }

    /** Marks the current request's thread; work handed to other threads is not marked. */
    static void enter(boolean readOnly) {
        if (readOnly) {
            READ_ONLY.set(Boolean.TRUE);
        } else {
            READ_ONLY.remove();
        }
    }

    static void leave() {
        READ_ONLY.remove();
    }

    static boolean readOnly() {
        return Boolean.TRUE.equals(READ_ONLY.get());
    }
}
