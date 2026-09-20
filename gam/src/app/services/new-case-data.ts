import { Injectable, signal } from '@angular/core';

@Injectable({
  providedIn: 'root',
})
export class NewCaseData {
  readonly isModalOpen = signal(false);
  /** A monotonically increasing event token for completed case creation. */
  readonly caseCreated = signal(0);

  openModal(): void {
    this.isModalOpen.set(true);
  }

  closeModal(): void {
    this.isModalOpen.set(false);
  }

  notifyCaseCreated(): void {
    this.caseCreated.update((version) => version + 1);
  }
}
