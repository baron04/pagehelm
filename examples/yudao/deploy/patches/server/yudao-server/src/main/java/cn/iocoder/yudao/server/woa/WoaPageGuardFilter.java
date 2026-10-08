package cn.iocoder.yudao.server.woa;

import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import org.springframework.boot.web.servlet.FilterRegistrationBean;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.core.Ordered;
import org.springframework.web.filter.OncePerRequestFilter;

import java.io.IOException;

/**
 * Marks a request read-only when it comes from the page the assistant is working and its
 * signed-in user has a window open.
 *
 * Only requests that say they come from that page are held ({@link #HEADER}, set by the
 * page itself while the assistant works it). The same person's other tabs and devices,
 * and their own actions on that page before and after, send no such header and are never
 * refused. Registered last, so it runs after sign-in is resolved.
 */
@Configuration
public class WoaPageGuardFilter {

    static final String PREFIX = "/admin-api/woa/page-guard/";
    static final String HEADER = "X-WOA-Assistant";

    @Bean
    public FilterRegistrationBean<OncePerRequestFilter> woaPageGuardFilterRegistration(WoaPageGuard guard) {
        OncePerRequestFilter filter = new OncePerRequestFilter() {
            @Override
            protected void doFilterInternal(HttpServletRequest request, HttpServletResponse response,
                                            FilterChain chain) throws ServletException, IOException {
                boolean own = request.getRequestURI().startsWith(PREFIX);
                boolean working = "1".equals(request.getHeader(HEADER));
                WoaPageGuard.enter(!own && working && guard.expiresAt() > 0);
                try {
                    chain.doFilter(request, response);
                } finally {
                    WoaPageGuard.leave();
                }
            }
        };
        FilterRegistrationBean<OncePerRequestFilter> registration = new FilterRegistrationBean<>(filter);
        registration.setOrder(Ordered.LOWEST_PRECEDENCE);
        registration.addUrlPatterns("/admin-api/*", "/app-api/*");
        return registration;
    }
}
