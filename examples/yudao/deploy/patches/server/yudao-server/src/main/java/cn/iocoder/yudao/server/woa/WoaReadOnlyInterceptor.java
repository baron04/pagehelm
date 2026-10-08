package cn.iocoder.yudao.server.woa;

import cn.iocoder.yudao.framework.common.exception.ServiceException;
import org.apache.ibatis.executor.Executor;
import org.apache.ibatis.mapping.MappedStatement;
import org.apache.ibatis.plugin.Interceptor;
import org.apache.ibatis.plugin.Intercepts;
import org.apache.ibatis.plugin.Invocation;
import org.apache.ibatis.plugin.Signature;
import org.springframework.stereotype.Component;

/**
 * Refuses every insert, update and delete of a request marked read-only.
 *
 * It sits on the one path all mapped statements take to the database, so a handler that
 * writes behind a GET, a nested service call or a generated mapper is refused the same.
 */
@Component
@Intercepts(@Signature(type = Executor.class, method = "update", args = {MappedStatement.class, Object.class}))
public class WoaReadOnlyInterceptor implements Interceptor {

    /** The code the assistant's proxy and the page both see for a refused write. */
    public static final int CODE = 1_900_000_001;

    @Override
    public Object intercept(Invocation invocation) throws Throwable {
        if (WoaPageGuard.readOnly()) {
            throw new ServiceException(CODE, "助手正在操作页面，这段时间内不能保存或修改数据");
        }
        return invocation.proceed();
    }
}
