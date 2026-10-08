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
 * Marks a request read-only when its signed-in user has a window open.
 *
 * Registered last, so it runs after sign-in is resolved. The guard's own endpoints are
 * left out: they write nothing to the database, and the window must stay closable.
 */
@Configuration
public class WoaPageGuardFilter {

    static final String PREFIX = "/admin-api/woa/page-guard/";

    @Bean
    public FilterRegistrationBean<OncePerRequestFilter> woaPageGuardFilterRegistration(WoaPageGuard guard) {
        OncePerRequestFilter filter = new OncePerRequestFilter() {
            @Override
            protected void doFilterInternal(HttpServletRequest request, HttpServletResponse response,
                                            FilterChain chain) throws ServletException, IOException {
                boolean own = request.getRequestURI().startsWith(PREFIX);
                WoaPageGuard.enter(!own && guard.expiresAt() > 0);
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
