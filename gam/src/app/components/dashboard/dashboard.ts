import { Component } from '@angular/core';
import { NewFraudCase } from '../new-fraud-case/new-fraud-case';
import { NewCaseData } from '../../services/new-case-data';
import { AuthService } from '../../services/auth/auth-service';

@Component({
  selector: 'app-dashboard',
  standalone: true,
  imports: [NewFraudCase],
  templateUrl: './dashboard.html',
  styleUrls: ['./dashboard.scss'],
})
export class Dashboard {

  // Stat card data
  totalFraudCases: number = 128;
  casesMissingData: number = 34;
  casesCompletedTransactions: number = 79;
  casesIncompleteNodes: number = 22;

  constructor(
    private newCaseData: NewCaseData,
    private auth: AuthService,
  ) {}

  /** Calls backend to clear the HttpOnly cookie, then navigates to /login. */
  signOut(): void {
    this.auth.logout();
  }

  // Calls service to open modal — no more showNewCaseModal boolean here
  startNewCase(): void {
    this.newCaseData.openModal();
  }
}

