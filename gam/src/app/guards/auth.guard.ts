import { inject } from '@angular/core';
import { CanActivateFn, Router } from '@angular/router';
import { map } from 'rxjs/operators';
import { AuthService } from '../services/auth/auth-service';

/**
 * `authGuard` — protects routes that require an authenticated session.
 *
 * Used on: /dashboard, /newFraudCase
 *
 * On every activation it calls `AuthService.checkSession()` which hits
 * `GET /auth/me` on the backend to validate the HttpOnly cookie.
 *
 *   • Valid session   → allow navigation (return true)
 *   • No / bad session → redirect to /login (return UrlTree)
 *
 * Why call the backend every time?
 * ---------------------------------
 * The `access_token` is an HttpOnly cookie — JavaScript cannot read it.
 * We therefore cannot locally check expiry.  Hitting /auth/me is the only
 * reliable way to know whether the session is still alive (e.g. the token
 * may have been revoked or expired server-side between navigations).
 */
export const authGuard: CanActivateFn = () => {
  const auth   = inject(AuthService);
  const router = inject(Router);

  return auth.checkSession().pipe(
    map((isAuth) => isAuth || router.createUrlTree(['/login'])),
  );
};
