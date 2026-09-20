import { Component, effect, inject, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { HttpClient } from '@angular/common/http';
import { NewFraudCase } from '../new-fraud-case/new-fraud-case';
import { NewCaseData } from '../../services/new-case-data';
import { AuthService } from '../../services/auth/auth-service';
import { environment } from '../../environment';
import { RouterLink } from '@angular/router';

interface DashboardSummary {
  totalFraudCases: number;
  uploadsInProgress: number;
  completedUploads: number;
  failedUploads: number;
}

interface FraudCaseCard {
  uploadId: string;
  inspectorName: string;
  createdAt: string;
}

@Component({
  selector: 'app-dashboard',
  standalone: true,
  imports: [CommonModule, RouterLink, NewFraudCase],
  templateUrl: './dashboard.html',
  styleUrls: ['./dashboard.scss'],
})
export class Dashboard {

  readonly totalFraudCases = signal(0);
  readonly uploadsInProgress = signal(0);
  readonly completedUploads = signal(0);
  readonly failedUploads = signal(0);
  readonly isLoading = signal(true);
  readonly loadError = signal(false);
  readonly cases = signal<FraudCaseCard[]>([]);

  private http = inject(HttpClient);

  constructor(
    private newCaseData: NewCaseData,
    private auth: AuthService,
  ) {
    effect(() => {
      this.newCaseData.caseCreated();
      this.loadDashboard();
    });
  }

  private loadDashboard(): void {
    this.isLoading.set(true);
    this.loadError.set(false);
    this.http
      .get<DashboardSummary>(`${environment.apiBaseUrl}/api/dashboard/summary`)
      .subscribe({
        next: (summary) => {
          this.totalFraudCases.set(summary.totalFraudCases);
          this.uploadsInProgress.set(summary.uploadsInProgress);
          this.completedUploads.set(summary.completedUploads);
          this.failedUploads.set(summary.failedUploads);
          this.isLoading.set(false);
        },
        error: (error) => {
          console.error('Failed to load dashboard summary:', error);
          this.loadError.set(true);
          this.isLoading.set(false);
        },
      });

    this.http
      .get<{ cases: FraudCaseCard[] }>(`${environment.apiBaseUrl}/api/dashboard/cases`)
      .subscribe({
        next: ({ cases }) => this.cases.set(cases),
        error: (error) => {
          console.error('Failed to load dashboard cases:', error);
          this.loadError.set(true);
        },
      });
  }

  /** Calls backend to clear the HttpOnly cookie, then navigates to /login. */
  signOut(): void {
    this.auth.logout();
  }

  // Calls service to open modal — no more showNewCaseModal boolean here
  startNewCase(): void {
    this.newCaseData.openModal();
  }
}
