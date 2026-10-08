package cn.iocoder.yudao.server.woa;

import cn.iocoder.yudao.framework.common.exception.ServiceException;
import cn.iocoder.yudao.framework.common.pojo.CommonResult;
import cn.iocoder.yudao.framework.web.core.handler.GlobalExceptionHandler;
import jakarta.servlet.http.HttpServletRequest;
import org.mybatis.spring.MyBatisSystemException;
import org.springframework.core.annotation.Order;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;

/**
 * Says why a write was refused inside the assistant's window.
 *
 * The refusal is raised where mapped statements pass, so it reaches the web layer wrapped
 * as a persistence failure and would be reported as an unexplained system error. This
 * unwraps that one refusal and answers with its own code and message; every other
 * persistence failure goes on to the application's handler unchanged.
 */
@RestControllerAdvice
@Order(-1)
public class WoaPageGuardAdvice {

    private final GlobalExceptionHandler handler;

    public WoaPageGuardAdvice(GlobalExceptionHandler handler) {
        this.handler = handler;
    }

    @ExceptionHandler(MyBatisSystemException.class)
    public CommonResult<?> refusedWrite(HttpServletRequest request, MyBatisSystemException ex) {
        for (Throwable cause = ex; cause != null && cause != cause.getCause(); cause = cause.getCause()) {
            if (cause instanceof ServiceException refused
                    && Integer.valueOf(WoaReadOnlyInterceptor.CODE).equals(refused.getCode())) {
                return CommonResult.error(refused.getCode(), refused.getMessage());
            }
        }
        return handler.allExceptionHandler(request, ex);
    }
}
