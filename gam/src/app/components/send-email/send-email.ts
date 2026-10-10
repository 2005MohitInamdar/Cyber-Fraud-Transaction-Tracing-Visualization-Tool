import { Component, OnInit, inject, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { HttpClient } from '@angular/common/http';
import { ActivatedRoute, RouterLink } from '@angular/router';

const API = 'http://localhost:8000';
const EMAIL_RE = /^[^\s@,<>]+@[^\s@,<>]+\.[A-Za-z]{2,}$/;

interface Preview {
  ackNo: string; itemCount: number; itemHash: string;
  subject: string; bodyText: string; testMode: boolean;
}

@Component({
  selector: 'app-send-email',
  standalone:true,
  imports: [CommonModule, FormsModule, RouterLink],
  templateUrl: './send-email.html',
  styleUrls: ['./send-email.scss'],
})
export class SendEmail implements OnInit {
   private readonly route = inject(ActivatedRoute);
  private readonly http = inject(HttpClient);

  uploadId = '';
  replyTo = '';
  ackNo = '';
  to = '';

  readonly step = signal<'setup' | 'loading' | 'review' | 'sent'>('setup');
  readonly preview = signal<Preview | null>(null);
  readonly error = signal<string | null>(null);
  readonly sending = signal(false);

  ngOnInit(): void {
    this.uploadId = this.route.snapshot.paramMap.get('uploadId') ?? '';
    this.ackNo = this.route.snapshot.queryParamMap.get('ackNo') ?? '';
  }

  generate(): void {
    if (!EMAIL_RE.test(this.replyTo.trim())) {
      this.error.set('Enter a valid reply-to email address.');
      return;
    }
    this.error.set(null);
    this.step.set('loading');
    this.http.post<Preview>(
      `${API}/api/cases/${this.uploadId}/requisitions/preview`,
      { replyTo: this.replyTo.trim(), ackNo: this.ackNo.trim() || null },
      { withCredentials: true },
    ).subscribe({
      next: (p) => { this.preview.set(p); this.ackNo = p.ackNo; this.step.set('review'); },
      error: (e) => {
        this.error.set(e?.error?.detail ?? 'Could not generate the email.');
        this.step.set('setup');
      },
    });
  }

  get addresses(): string[] {
    return this.to.split(',').map((a) => a.trim()).filter(Boolean);
  }
  get canSend(): boolean {
    const a = this.addresses;
    return a.length > 0 && a.length <= 5 && a.every((x) => EMAIL_RE.test(x));
  }

  send(): void {
    const p = this.preview();
    if (!p || !this.canSend) return;
    this.sending.set(true);
    this.error.set(null);
    this.http.post(
      `${API}/api/cases/${this.uploadId}/requisitions/send`,
      { replyTo: this.replyTo.trim(), ackNo: this.ackNo, to: this.addresses, itemHash: p.itemHash },
      { withCredentials: true },
    ).subscribe({
      next: () => { this.sending.set(false); this.step.set('sent'); },
      error: (e) => {
        this.error.set(e?.error?.detail ?? 'Sending failed.');
        this.sending.set(false);
      },
    });
  }
}
