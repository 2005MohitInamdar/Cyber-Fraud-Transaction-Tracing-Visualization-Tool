import { inject } from '@angular/core';
import { CanActivateFn, Router } from '@angular/router';
import { map } from 'rxjs/operators';
import { AuthService } from '../services/auth/auth-service';

/**
 * `guestGuard` — protects routes that should only be visible to guests
 * (unauthenticated users).
 *
 * Used on: /login, /signup, /checkEmail
 *
 * Calls `AuthService.checkSession()` to verify the current cookie state:
 *
 *   • Valid session   → user is already logged in → redirect to /dashboard
 *   • No / bad session → allow navigation (return true)
 *
 * This prevents an already-authenticated user from accidentally navigating
 * back to the login page (e.g. via the browser back button) and seeing a
 * blank form instead of their dashboard.
 */
export const guestGuard: CanActivateFn = () => {
  const auth = inject(AuthService);
  const router = inject(Router);

  return auth.checkSession().pipe(
    map((isAuth) => isAuth ? router.createUrlTree(['/dashboard']) : true),
  );
};
