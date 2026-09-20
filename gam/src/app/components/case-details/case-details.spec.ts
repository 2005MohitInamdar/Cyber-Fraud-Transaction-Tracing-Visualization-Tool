import { ComponentFixture, TestBed } from '@angular/core/testing';

import { CaseDetails } from './case-details';

describe('CaseDetails', () => {
  let component: CaseDetails;
  let fixture: ComponentFixture<CaseDetails>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [CaseDetails],
    }).compileComponents();

    fixture = TestBed.createComponent(CaseDetails);
    component = fixture.componentInstance;
    await fixture.whenStable();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });
});
