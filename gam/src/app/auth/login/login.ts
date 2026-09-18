import { Component, inject } from '@angular/core';
import { RouterLink } from '@angular/router';
import { ReactiveFormsModule, FormGroup, FormControl, Validators } from '@angular/forms';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { environment } from '../../environment';
import { Router } from '@angular/router';
import { AuthService } from '../../services/auth/auth-service';
@Component({
  selector: 'app-login',
  standalone: true,
  imports: [RouterLink, ReactiveFormsModule],
  templateUrl: './login.html',
  styleUrl: './login.scss',
})


export class Login {
  private http = inject(HttpClient);
  private router = inject(Router);
  private auth = inject(AuthService);

  form = new FormGroup({
    email: new FormControl('', [Validators.required, Validators.email]),
    password: new FormControl('', [Validators.required, Validators.minLength(4)]),
  });

  isLoading = false;
  errorMessage = '';
  successMessage = '';

  get email() { return this.form.get('email')!; }
  get password() { return this.form.get('password')!; }

  async onLogin(): Promise<void> {
    if (this.form.invalid) return;

    this.errorMessage = '';
    this.successMessage = '';
    this.isLoading = true;

    try {
      // Simulate network latency for UI testing
      await new Promise((resolve) => setTimeout(resolve, 1500));

      const response = await firstValueFrom(
        this.http.post<{ message: string }>(`${environment.apiBaseUrl}/auth/loginUser`, {
          email: this.email.value,
          password: this.password.value,
        }, { withCredentials: true }   //for cookies
      )
      );

      this.successMessage = response?.message || 'Logged in successfully!';

      // Stamp auth state NOW so authGuard uses cached value instead of
      // racing a fresh /auth/me call against the brand-new cookie.
      this.auth.setAuthenticated();

      await this.router.navigate(['/dashboard']);

    } catch (error: any) {
      console.error('Login failed:', error);
      this.errorMessage =
        error?.error?.detail ?? 'Login failed. Please check your credentials.';
    } finally {
      this.isLoading = false;
    }
  }
}
