import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Router } from '@angular/router';
import { BehaviorSubject, Observable, of } from 'rxjs';
import { map, catchError, tap } from 'rxjs/operators';
import { environment } from '../../../environments/environment';

export interface AuthUser {
  userId: string;
}


@Injectable({
  providedIn: 'root',
})
export class AuthService {
  private http   = inject(HttpClient);
    private router = inject(Router);
  
    // null  → unknown / not yet checked
    // false → definitely not authenticated
    // AuthUser → authenticated
    private _user$ = new BehaviorSubject<AuthUser | null | false>(null);
  
    /** Emits the current auth state (null = loading, false = guest, AuthUser = logged-in). */
    readonly user$ = this._user$.asObservable();
  
    /** Convenience: true when a valid session exists in the current BehaviorSubject snapshot. */
    get isAuthenticated(): boolean {
      const v = this._user$.getValue();
      return v !== null && v !== false;
    }
  
    /**
     * Asks the backend whether the cookie is still valid.
     *
     * Returns an Observable<boolean>:
     *   true  → session is valid, `_user$` updated with user info
     *   false → no session, `_user$` set to false
     *
     * Guards should call this and route based on the emitted boolean.
     */
    /**
     * Called immediately after a successful login response so that guards
     * can trust the cached state and skip the round-trip to /auth/me.
     */
    setAuthenticated(user: AuthUser = { userId: 'authenticated' }): void {
      this._user$.next(user);
    }

    checkSession(): Observable<boolean> {
      // Fast-path: if we already have a confirmed user in memory, trust it.
      const cached = this._user$.getValue();
      if (cached !== null && cached !== false) {
        return of(true);
      }

      return this.http
        .get<AuthUser>(`${environment.apiBaseUrl}/auth/me`)
        .pipe(
          tap((user) => this._user$.next(user)),
          map(() => true),
          catchError(() => {
            this._user$.next(false);
            return of(false);
          }),
        );
    }
  
    /**
     * Calls `POST /auth/logout` to clear the HttpOnly cookie server-side,
     * then resets local state and navigates to /login.
     */
    logout(): void {
      this.http
        .post<{ message: string }>(`${environment.apiBaseUrl}/auth/logout`, {})
        .pipe(catchError(() => of(null)))
        .subscribe(() => {
          this._user$.next(false);
          this.router.navigate(['/login']);
        });
    }
}
