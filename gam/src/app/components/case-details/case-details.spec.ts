import { ComponentFixture, TestBed } from '@angular/core/testing';
import { ActivatedRoute, provideRouter } from '@angular/router';
import { Case_Details } from '../../services/case_Details/case-details';

import { CaseDetails } from './case-details';

describe('CaseDetails', () => {
  let component: CaseDetails;
  let fixture: ComponentFixture<CaseDetails>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [CaseDetails],
      providers: [
        provideRouter([]),
        { provide: ActivatedRoute, useValue: { snapshot: { paramMap: { get: () => null } } } },
        { provide: Case_Details, useValue: {} },
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(CaseDetails);
    component = fixture.componentInstance;
    await fixture.whenStable();
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });
});
