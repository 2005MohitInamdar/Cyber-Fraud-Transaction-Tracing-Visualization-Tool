import { ComponentFixture, TestBed } from '@angular/core/testing';

import { SendEmail } from './send-email';

describe('SendEmail', () => {
  let component: SendEmail;
  let fixture: ComponentFixture<SendEmail>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [SendEmail],
    }).compileComponents();

    fixture = TestBed.createComponent(SendEmail);
    component = fixture.componentInstance;
    await fixture.whenStable();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });
});
