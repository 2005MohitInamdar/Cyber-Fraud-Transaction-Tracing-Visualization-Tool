import { Routes } from '@angular/router';
import { Login } from './auth/login/login';
import { Signup } from './auth/signup/signup';
import { CheckEmail } from './auth/check-email/check-email';
import { Dashboard } from './components/dashboard/dashboard';
import { NewFraudCase } from './components/new-fraud-case/new-fraud-case';
import { authGuard } from './guards/auth.guard';
import { guestGuard } from './guards/guest.guard';

export const routes: Routes = [
  { path: '', redirectTo: 'login', pathMatch: 'full' },

  // ── Guest-only routes (redirect to /dashboard if already logged in) ──────
  { path: 'login',      component: Login,       canActivate: [guestGuard] },
  { path: 'signup',     component: Signup,      canActivate: [guestGuard] },
  { path: 'checkEmail', component: CheckEmail,  canActivate: [guestGuard] },

  // ── Protected routes (redirect to /login if not authenticated) ───────────
  { path: 'dashboard',    component: Dashboard,    canActivate: [authGuard] },
  { path: 'newFraudCase', component: NewFraudCase, canActivate: [authGuard] },
];

