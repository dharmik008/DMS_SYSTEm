from flask import Blueprint, render_template, request, redirect, url_for, flash, session, g, current_app, jsonify
from functools import wraps
import os
import uuid
from werkzeug.utils import secure_filename
from datetime import datetime as _datetime, timedelta as _timedelta, timezone as _tz

_IST = _tz(_timedelta(hours=5, minutes=30))


def _now_ist():
    """Return current IST time as a naive datetime."""
    return _datetime.now(_IST).replace(tzinfo=None)

from db import (
    user_get_by_id, vehicles_get_by_dealer, vehicles_inventory_summary,
    vehicles_get_fuel_breakdown, vehicle_create, vehicle_update, vehicle_delete, vehicle_get,
    leads_get_by_dealer, leads_get_stage_counts, lead_create, lead_update, lead_delete, lead_get,
    agents_get_by_dealer, agents_get_leads_count, agent_create, agent_update, agent_delete, agent_get,
    deals_get_by_dealer, deals_get_recent, deals_get_status_counts, deals_get_financial_summary,
    deals_get_monthly_revenue, deal_create, deal_update, deal_get, leads_get_source_counts,
    documents_get_by_dealer, document_create, document_delete,
    inquiries_get_by_dealer, inquiry_update_status,
    user_update_subscription,
    # v29.2 expense/profit helpers
    expense_create, expense_get, expenses_get_by_vehicle, expense_update, expense_delete,
    vehicle_profit_summary, inventory_profit_dashboard,
)
from models import db, User, Vehicle, VehicleImage
from utils.image_utils import save_uploaded_image, is_allowed_image
from utils.vehicle_issues import detect_vehicle_issues
from subscription_features import feature_required
from subscription_gate import (
    subscription_required, subscription_state, activate_plan_for_dealer,
    get_active_subscription, get_latest_subscription,
)
from plan_quotas import quota_required, check_quota, quota_summary
from subscription_gate import current_plan_key

dealer_bp = Blueprint('dealer', __name__)


def log_dealer_action(action, module, status='Success', description=None):
    """Log dealer activity into the unified AdminLog table."""
    try:
        from extensions import db
        from models import AdminLog
        from flask import request as _req, g as _g
        from utils.request_meta import get_request_meta
        dealer_name = _g.user.get('name', 'Dealer') if _g.user else 'Dealer'
        dealer_id = _g.user.get('id') if _g.user else None
        ip, browser, os_name, device = get_request_meta(_req)
        log = AdminLog(
            user_id=dealer_id,
            admin_user=dealer_name,
            user_role='Dealer',
            action=action,
            module=module,
            description=description or action,
            ip_address=ip,
            device=device,
            browser=browser,
            timezone='Asia/Kolkata (IST)',
            status=status,
        )
        db.session.add(log)
        db.session.commit()
    except Exception:
        pass


def dealer_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        # Only the dealer token is touched here: a buyer or admin signed in on
        # the same browser stays signed in.
        from utils.auth_tokens import COOKIE, sign_out
        if not g.user or g.user.get('role') != 'dealer':
            if request.cookies.get(COOKIE['dealer']):
                # A dealer token was present but its account no longer exists.
                sign_out('dealer')
                flash('Your account has been deleted. Please contact the admin.', 'error')
            else:
                flash('Please sign in as a dealer to continue.', 'info')
            return redirect(url_for('auth.login'))
        # ── Block suspended dealer even if signed in ─────────────────────────
        if not g.user.get('is_active', True):
            sign_out('dealer')
            flash('Your account has been suspended. Please contact the admin.', 'error')
            return redirect(url_for('auth.login'))
        return f(*args, **kwargs)
    return decorated


# ── KYC restriction: dealers without complete KYC are redirected ──────────────
# Allowed routes when KYC is incomplete (endpoint names)
_KYC_EXEMPT_ENDPOINTS = {
    'dealer.kyc_upload',     # KYC upload page
    'dealer.kyc_submit',     # KYC form POST
    'auth.logout',           # allow logout
}


def kyc_required(f):
    """Decorator that blocks access until ALL 3 KYC documents are approved by admin."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not g.user or g.user.get('role') != 'dealer':
            return f(*args, **kwargs)
        from models import DealerKYC
        dealer_id = g.user['id']
        kyc = DealerKYC.query.filter_by(dealer_id=dealer_id).first()

        docs_uploaded = (
            kyc is not None and
            kyc.aadhaar_front and
            kyc.aadhaar_back and
            kyc.pan_card
        )

        # Only fully approved when ALL 3 per-doc statuses are 'approved'
        all_approved = (
            docs_uploaded and
            getattr(kyc, 'aadhaar_front_status', 'pending') == 'approved' and
            getattr(kyc, 'aadhaar_back_status', 'pending') == 'approved' and
            getattr(kyc, 'pan_card_status', 'pending') == 'approved'
        )

        if not all_approved:
            if docs_uploaded:
                # Check if any doc is rejected
                rejected = [
                    d for d in ('aadhaar_front', 'aadhaar_back', 'pan_card')
                    if getattr(kyc, d + '_status', 'pending') == 'rejected'
                ]
                if rejected:
                    doc_names = {'aadhaar_front': 'Aadhaar Front', 'aadhaar_back': 'Aadhaar Back', 'pan_card': 'PAN Card'}
                    names = ', '.join(doc_names[d] for d in rejected)
                    flash(f'Some KYC documents were rejected ({names}). Please re-upload them to continue.', 'error')
                else:
                    flash('Your KYC documents are under review. Please wait for admin approval.', 'info')
            else:
                flash('Complete your KYC verification to continue using the DMS.', 'warning')
            return redirect(url_for('dealer.kyc_upload'))
        return f(*args, **kwargs)
    return decorated


def get_dealer_id():
    return g.user['id'] if g.user else None

# ========== KYC UPLOAD (dealer-side) ==========


@dealer_bp.route('/kyc', methods=['GET'])
@dealer_required
def kyc_upload():
    """Show KYC document upload page for dealers with incomplete KYC."""
    from models import DealerKYC, DealerNotification
    dealer_id = get_dealer_id()
    kyc = DealerKYC.query.filter_by(dealer_id=dealer_id).first()

    # If all 3 docs are already approved, redirect straight to dashboard
    if (kyc and
            getattr(kyc, 'aadhaar_front_status', 'pending') == 'approved' and
            getattr(kyc, 'aadhaar_back_status', 'pending') == 'approved' and
            getattr(kyc, 'pan_card_status', 'pending') == 'approved'):
        return redirect(url_for('dealer.dashboard'))

    notifications = DealerNotification.query.filter_by(
        dealer_id=dealer_id).order_by(DealerNotification.created_at.desc()).limit(10).all()
    return render_template('dealer/kyc_upload.html', kyc=kyc, notifications=notifications)


@dealer_bp.route('/api/notifications/mark-read', methods=['POST'])
@dealer_required
def mark_notifications_read():
    """Mark all dealer notifications as read."""
    from models import DealerNotification
    dealer_id = get_dealer_id()
    DealerNotification.query.filter_by(dealer_id=dealer_id, is_read=False).update({'is_read': True})
    db.session.commit()
    return jsonify({'success': True})


@dealer_bp.route('/kyc/submit', methods=['POST'])
@dealer_required
def kyc_submit():
    """Handle dealer KYC document upload."""
    from models import DealerKYC
    from utils.upload_helpers import save_image, validate_image
    dealer_id = get_dealer_id()

    kyc = DealerKYC.query.filter_by(dealer_id=dealer_id).first()
    if not kyc:
        kyc = DealerKYC(dealer_id=dealer_id, kyc_status='pending')
        db.session.add(kyc)
    from datetime import datetime
    kyc.submitted_at = _now_ist()

    folder = os.path.join(
        current_app.config.get('KYC_UPLOAD_FOLDER',
                               os.path.join(os.path.dirname(__file__), '..', 'static', 'uploads', 'dealers')),
        str(dealer_id)
    )
    os.makedirs(folder, exist_ok=True)
    errors = []

    for doc_key in ('aadhaar_front', 'aadhaar_back', 'pan_card'):
        f = request.files.get(doc_key)
        if not f or not f.filename:
            continue
        # Don't overwrite an already-approved document
        current_status = getattr(kyc, doc_key + '_status', 'pending') or 'pending'
        if current_status == 'approved':
            flash(f'{doc_key.replace("_"," ").title()} is already approved — skipped.', 'info')
            continue
        ok, err = validate_image(f)
        if not ok:
            errors.append(f'{doc_key}: {err}')
            continue
        saved = save_image(f, folder, prefix=doc_key.replace('_', '-'), vehicle_mode=False)
        if saved:
            setattr(kyc, doc_key, saved)
            # Reset this doc's status back to pending (awaiting re-review)
            setattr(kyc, doc_key + '_status', 'pending')
            setattr(kyc, doc_key + '_reject', None)
            # ── Register in Centralized Document Storage ──────────────────────
            try:
                from db import cds_register
                cds_register({
                    'dealer_id':     dealer_id,
                    'file_name':     saved,
                    'original_name': f.filename,
                    'file_path':     os.path.join('uploads', 'dealers', str(dealer_id), saved),
                    'module_name':   'KYC',
                    'document_type': doc_key.replace('_', ' ').title(),
                    'uploaded_by':   dealer_id,
                    'performed_by':  f'dealer:{dealer_id}',
                })
            except Exception:
                pass  # Never break the main KYC flow
            # ─────────────────────────────────────────────────────────────────
        else:
            errors.append(f'Failed to save {doc_key}.')

    if errors:
        for e in errors:
            flash(e, 'error')
    else:
        flash('KYC documents uploaded successfully.', 'success')

    # Recalculate overall status based on per-doc statuses
    kyc.recalculate_status()
    db.session.commit()
    log_dealer_action('Submitted KYC documents', 'KYC',
                       status='Failed' if errors else 'Success')
    return redirect(url_for('dealer.kyc_upload'))


# ========== DASHBOARD ==========
@dealer_bp.route('/dashboard')
@dealer_required
@subscription_required   # v29: no plan → /dealer/subscription?onboarding=1
@kyc_required
def dashboard():
    dealer_id = get_dealer_id()

    inventory_summary = vehicles_inventory_summary(dealer_id)
    lead_stages = leads_get_stage_counts(dealer_id)
    recent_leads = leads_get_by_dealer(dealer_id, page=1, per_page=5)['items']
    recent_deals = deals_get_recent(dealer_id)
    financial = deals_get_financial_summary(dealer_id)
    # FIXED: fetch latest 5 inquiries so the dashboard widget can display them
    recent_inquiries = inquiries_get_by_dealer(dealer_id)[:5]
    pending_inquiries = sum(
        1 for i in recent_inquiries if i['status'] == 'pending')
    # v29.2: inventory profit summary cards
    profit_summary = inventory_profit_dashboard(dealer_id)

    return render_template('dealer/dashboard.html',
                           total_vehicles=inventory_summary['total'],
                           available=inventory_summary['available'],
                           total_leads=sum(lead_stages.values()),
                           new_leads=lead_stages.get('new', 0),
                           total_deals=financial['total_deals'],
                           delivered=financial['total_deals'],
                           revenue=financial['total_revenue'],
                           lead_stages=lead_stages,
                           recent_leads=recent_leads,
                           recent_deals=recent_deals,
                           recent_inquiries=recent_inquiries,       # FIXED: now passed to template
                           pending_inquiries=pending_inquiries,     # FIXED: badge count for pending
                           profit_summary=profit_summary,           # v29.2
                           )

# ========== INVENTORY ==========


@dealer_bp.route('/inventory')
@dealer_required
@kyc_required
def inventory():
    dealer_id = get_dealer_id()
    page = request.args.get('page', 1, type=int)
    status = request.args.get('status', '')
    fuel = request.args.get('fuel', '')
    search = request.args.get('search', '')
    # Set by the "View Vehicles" button on a dead-stock popup: show only
    # vehicles that have been on the lot longer than N months.
    aging = request.args.get('aging', 0, type=int)

    vehicles = vehicles_get_by_dealer(
        dealer_id, status=status, fuel=fuel, search=search, page=page, aging=aging,
        exclude_sold=True)

    # Enrich each vehicle with dynamically computed issue data
    for v in vehicles['items']:
        v['vehicle_issues'] = detect_vehicle_issues(v)

    return render_template('dealer/inventory.html', vehicles=vehicles, status_filter=status,
                           fuel_filter=fuel, search=search, aging_filter=aging,
                           quota=check_quota(dealer_id, current_plan_key(), 'vehicles', adding=0))


@dealer_bp.route('/api/vehicle/<int:vehicle_id>/contacts')
@dealer_required
def vehicle_contacts_api(vehicle_id):
    """
    People who already raised a lead or an inquiry about this vehicle.

    Feeds the suggestion list in Customer Info on the New Deal page.

    The dealer_id comes from the session, and vehicle ownership is checked
    before anything is read, so a dealer cannot reach another rooftop's
    contacts by passing a vehicle id that is not theirs.
    """
    from utils.vehicle_contacts import vehicle_contacts, dealer_owns_vehicle

    dealer_id = get_dealer_id()
    try:
        if not dealer_owns_vehicle(dealer_id, vehicle_id):
            return jsonify({'contacts': [], 'error': 'not_found'}), 404
        contacts = vehicle_contacts(dealer_id, vehicle_id)
    except Exception as exc:        # a lookup must never block the deal form
        current_app.logger.warning('vehicle contacts lookup failed: %s', exc)
        db.session.rollback()
        return jsonify({'contacts': []}), 200

    return jsonify({'contacts': contacts, 'count': len(contacts)}), 200


@dealer_bp.route('/api/inventory-alerts')
@dealer_required
def inventory_alerts_api():
    """
    Returns the popups this dealer should see right now, and consumes the
    crossings that produced them.

    Scope: a dealer only ever receives alerts for their own rooftop — the
    dealer_id comes from the session, never from the request.
    """
    from utils.inventory_alerts import alerts_for_page, inventory_snapshot

    dealer_id = get_dealer_id()
    try:
        # alerts_for_page prefers a fresh crossing, and otherwise reports the
        # standing condition, so low stock is surfaced on every visit.
        alerts = alerts_for_page(dealer_id)
        snapshot = inventory_snapshot(dealer_id)
    except Exception as exc:          # never let alerting break the page
        current_app.logger.warning('inventory alert evaluation failed: %s', exc)
        db.session.rollback()
        return jsonify({'alerts': [], 'snapshot': None}), 200

    return jsonify({'alerts': alerts, 'snapshot': snapshot}), 200



@dealer_bp.route('/inventory/add', methods=['GET', 'POST'])
@dealer_required
@kyc_required
@quota_required('vehicles')
def add_vehicle():
    dealer_id = get_dealer_id()

    if request.method == 'POST':
        # ── Server-side: require all 7 mandatory image slots ─────────────────────
        MANDATORY_SLOTS = [
            ('img_front',      'front',      0),
            ('img_rear',       'rear',       1),
            ('img_right_side', 'right_side', 2),
            ('img_left_side',  'left_side',  3),
            ('img_engine',     'engine',     4),
            ('img_boot',       'boot',       5),
            ('img_interior',   'interior',   6),
        ]
        SLOT_LABELS = {
            'front': 'Front View', 'rear': 'Rear / Tail View',
            'right_side': 'Right Side View', 'left_side': 'Left Side View',
            'engine': 'Engine Bay', 'boot': 'Boot / Trunk', 'interior': 'Interior',
        }
        missing_slots = []
        for field_name, img_type, _ in MANDATORY_SLOTS:
            f = request.files.get(field_name)
            if not f or not f.filename or not is_allowed_image(f.filename):
                missing_slots.append(SLOT_LABELS.get(img_type, img_type))

        if missing_slots:
            flash(
                'Please upload all required vehicle photos before submitting: '
                + ', '.join(missing_slots),
                'error'
            )
            return render_template('dealer/vehicle_form.html', action='Add', vehicle=None)

        # ── Primary image: first mandatory slot (front) or explicit 'image' field ──
        # Try 'image' field first (explicit primary); fall back to 'front' slot
        image_file = request.files.get('image') or request.files.get('img_front')
        filename = 'default_car.jpg'

        if image_file and image_file.filename and is_allowed_image(image_file.filename):
            filename = save_uploaded_image(
                image_file, current_app.config['UPLOAD_FOLDER'], uuid.uuid4().hex)

        vehicle_data = {
            'dealer_id': dealer_id,
            'make': request.form.get('make'),
            'model': request.form.get('model'),
            'variant': request.form.get('variant'),
            'year': int(request.form.get('year')),
            'color': request.form.get('color'),
            'fuel_type': request.form.get('fuel_type'),
            'transmission': request.form.get('transmission'),
            'mileage': int(request.form.get('mileage') or 0),
            'engine_cc': int(request.form.get('engine_cc') or 0),
            'price': float(request.form.get('price')),
            'negotiable': request.form.get('negotiable') == 'on',
            # engine_cc, condition, vin_number and rc_available were removed from
            # the dealer form. The columns stay — admin can still set them — so
            # these keep the values the old form defaulted to.
            'condition': request.form.get('condition') or 'used',
            'status': request.form.get('status'),
            'description': request.form.get('description'),
            'vin_number': (request.form.get('vin_number') or '').strip().upper(),
            'registration_number': (request.form.get('registration_number') or '').strip().upper(),
            'insurance_valid_till': request.form.get('insurance_valid_till'),
            'rc_available': request.form.get('rc_available', 'on') == 'on',
            'featured': request.form.get('featured') == 'on',
            'image_filename': filename,
            # new condition detail fields
            'accident_history':   request.form.get('accident_history', 'NA').strip(),
            'loan_status':        request.form.get('loan_status', 'NA').strip(),
            'rc_service_records': request.form.get('rc_service_records', 'NA').strip(),
            'major_issues':       ','.join(request.form.getlist('major_issues')) or 'None',
            'keys_available':     request.form.get('keys_available', 'NA').strip(),
            'body_panel_status':  request.form.get('body_panel_status', 'NA').strip(),
            # seller / customer this vehicle came from — every field optional
            'seller_name':    (request.form.get('seller_name') or '').strip(),
            'seller_phone':   (request.form.get('seller_phone') or '').strip(),
            'seller_email':   (request.form.get('seller_email') or '').strip().lower(),
            'seller_address': (request.form.get('seller_address') or '').strip(),
            'seller_city':    (request.form.get('seller_city') or '').strip(),
            # v29.2 cost/profit fields
            'purchase_price': float(request.form.get('purchase_price') or 0) or None,
        }

        vehicle_id = vehicle_create(vehicle_data)

        # ── Save typed mandatory images (7 slots) into vehicle_images ────────────
        # Order matches VehicleImage.MANDATORY_TYPES; front is already primary above
        # but we ALSO store it in vehicle_images so gallery always includes it
        # (MANDATORY_SLOTS already defined above for validation — reuse here)
        gallery_sort = 10  # gallery images start at sort_order 10+
        saved_any = False

        for field_name, img_type, sort_idx in MANDATORY_SLOTS:
            f = request.files.get(field_name)
            if not f or not f.filename:
                continue
            if not is_allowed_image(f.filename):
                continue
            try:
                fname = save_uploaded_image(
                    f, current_app.config['UPLOAD_FOLDER'], uuid.uuid4().hex)
                img_rec = VehicleImage(
                    vehicle_id=vehicle_id,
                    filename=fname,
                    image_type=img_type,
                    sort_order=sort_idx
                )
                db.session.add(img_rec)
                # If no explicit primary was uploaded, use first mandatory slot
                if img_type == 'front' and filename == 'default_car.jpg':
                    from models import Vehicle as _V
                    _v = _V.query.get(vehicle_id)
                    if _v:
                        _v.image_filename = fname
                saved_any = True
            except Exception as e:
                current_app.logger.error(f'add_vehicle mandatory image error: {e}')

        # ── Save additional gallery images ────────────────────────────────────────
        extra_files = request.files.getlist('extra_images')
        for f in extra_files[:10]:  # max 10 additional
            if not f or not f.filename:
                continue
            if not is_allowed_image(f.filename):
                continue
            try:
                fname = save_uploaded_image(
                    f, current_app.config['UPLOAD_FOLDER'], uuid.uuid4().hex)
                img_rec = VehicleImage(
                    vehicle_id=vehicle_id,
                    filename=fname,
                    image_type='gallery',
                    sort_order=gallery_sort
                )
                db.session.add(img_rec)
                gallery_sort += 1
                saved_any = True
            except Exception as e:
                current_app.logger.error(f'add_vehicle gallery image error: {e}')

        if saved_any:
            try:
                db.session.commit()
            except Exception as e:
                db.session.rollback()
                current_app.logger.error(f'add_vehicle image commit error: {e}')

        log_dealer_action(f'Added vehicle: {vehicle_data.get("make")} {vehicle_data.get("model")} {vehicle_data.get("year")}', 'Vehicles')
        flash('Vehicle added successfully', 'success')
        return redirect(url_for('dealer.inventory'))

    return render_template('dealer/vehicle_form.html', action='Add', vehicle=None)


@dealer_bp.route('/inventory/edit/<int:vid>', methods=['GET', 'POST'])
@dealer_required
@kyc_required
def edit_vehicle(vid):
    dealer_id = get_dealer_id()
    vehicle = vehicle_get(vid)

    if not vehicle or vehicle['dealer_id'] != dealer_id:
        flash('Vehicle not found', 'error')
        return redirect(url_for('dealer.inventory'))

    if request.method == 'POST':
        image_file = request.files.get('image')

        if image_file and image_file.filename and is_allowed_image(image_file.filename):
            new_fname = save_uploaded_image(
                image_file, current_app.config['UPLOAD_FOLDER'], uuid.uuid4().hex)
            vehicle['image_filename'] = new_fname

        update_data = {
            'make': request.form.get('make'),
            'model': request.form.get('model'),
            'variant': request.form.get('variant'),
            'year': int(request.form.get('year')),
            'color': request.form.get('color'),
            'fuel_type': request.form.get('fuel_type'),
            'transmission': request.form.get('transmission'),
            'mileage': int(request.form.get('mileage') or 0),
            # No longer on the dealer form, so nothing posts for these. Falling
            # back to the stored value means an edit never blanks an engine size,
            # VIN or condition that is already recorded.
            'engine_cc': int(request.form.get('engine_cc') or vehicle.get('engine_cc') or 0),
            'price': float(request.form.get('price')),
            'negotiable': request.form.get('negotiable') == 'on',
            'condition': request.form.get('condition') or vehicle.get('condition') or 'used',
            'status': request.form.get('status'),
            # Description was removed from the dealer form, but both customer-facing
            # car detail pages still show it. Fall back to what is stored so an edit
            # never blanks a live listing.
            'description': request.form.get('description') or vehicle.get('description'),
            'vin_number': (request.form.get('vin_number')
                           or vehicle.get('vin_number') or '').strip().upper(),
            'registration_number': (request.form.get('registration_number') or '').strip().upper(),
            'insurance_valid_till': request.form.get('insurance_valid_till'),
            # an absent checkbox looks the same as an unticked one, and the field
            # is gone from the form, so keep whatever is stored
            'rc_available': bool(vehicle.get('rc_available')),
            'featured': request.form.get('featured') == 'on',
            'image_filename': vehicle.get('image_filename', 'default_car.jpg'),
            # new condition detail fields
            # The Vehicle Condition Details section was removed from the dealer
            # form. Keep what is stored so an edit here does not wipe details an
            # admin recorded, and so the inventory issue badges survive.
            'accident_history':   (request.form.get('accident_history')
                                   or vehicle.get('accident_history') or 'NA').strip(),
            'loan_status':        (request.form.get('loan_status')
                                   or vehicle.get('loan_status') or 'NA').strip(),
            'rc_service_records': (request.form.get('rc_service_records')
                                   or vehicle.get('rc_service_records') or 'NA').strip(),
            'major_issues':       (','.join(request.form.getlist('major_issues'))
                                   or vehicle.get('major_issues') or 'None'),
            'keys_available':     (request.form.get('keys_available')
                                   or vehicle.get('keys_available') or 'NA').strip(),
            'body_panel_status':  (request.form.get('body_panel_status')
                                   or vehicle.get('body_panel_status') or 'NA').strip(),
            # seller / customer this vehicle came from — every field optional
            'seller_name':    (request.form.get('seller_name') or '').strip(),
            'seller_phone':   (request.form.get('seller_phone') or '').strip(),
            'seller_email':   (request.form.get('seller_email') or '').strip().lower(),
            'seller_address': (request.form.get('seller_address') or '').strip(),
            'seller_city':    (request.form.get('seller_city') or '').strip(),
            # v29.2 cost/profit fields
            'purchase_price': float(request.form.get('purchase_price') or 0) or None,
        }

        # ── Handle typed mandatory image uploads on edit ──────────────────────────
        MANDATORY_SLOTS = [
            ('img_front',      'front',      0),
            ('img_rear',       'rear',       1),
            ('img_right_side', 'right_side', 2),
            ('img_left_side',  'left_side',  3),
            ('img_engine',     'engine',     4),
            ('img_boot',       'boot',       5),
            ('img_interior',   'interior',   6),
        ]
        saved_any = False
        for field_name, img_type, sort_idx in MANDATORY_SLOTS:
            f = request.files.get(field_name)
            if not f or not f.filename or not is_allowed_image(f.filename):
                continue
            try:
                fname = save_uploaded_image(
                    f, current_app.config['UPLOAD_FOLDER'], uuid.uuid4().hex)
                # Check if a typed record already exists for this slot — replace it
                existing = VehicleImage.query.filter_by(
                    vehicle_id=vid, image_type=img_type).first()
                if existing:
                    existing.filename = fname
                else:
                    img_rec = VehicleImage(
                        vehicle_id=vid,
                        filename=fname,
                        image_type=img_type,
                        sort_order=sort_idx
                    )
                    db.session.add(img_rec)
                # If updating front image, also update the primary image_filename
                if img_type == 'front':
                    update_data['image_filename'] = fname
                saved_any = True
            except Exception as e:
                current_app.logger.error(f'edit_vehicle typed image error ({img_type}): {e}')

        if saved_any:
            try:
                db.session.commit()
            except Exception as e:
                db.session.rollback()
                current_app.logger.error(f'edit_vehicle image commit error: {e}')

        vehicle_update(vid, update_data)
        log_dealer_action(f'Updated vehicle: {update_data.get("make")} {update_data.get("model")} (ID:{vid})', 'Vehicles')
        flash('Vehicle updated successfully', 'success')
        return redirect(url_for('dealer.inventory'))

    # ── GET: load existing typed images for pre-filling the form ─────────────
    existing_typed_imgs = VehicleImage.query.filter_by(vehicle_id=vid).all()
    vehicle['extra_images_typed'] = [
        {'filename': img.filename, 'image_type': img.image_type, 'id': img.id}
        for img in existing_typed_imgs
    ]

    return render_template('dealer/vehicle_form.html', action='Edit', vehicle=vehicle)


@dealer_bp.route('/inventory/delete/<int:vid>', methods=['POST'])
@dealer_required
@kyc_required
def delete_vehicle(vid):
    dealer_id = get_dealer_id()
    vehicle = vehicle_get(vid)

    if vehicle and vehicle['dealer_id'] == dealer_id:
        vehicle_delete(vid)
        log_dealer_action(f'Deleted vehicle ID:{vid}', 'Vehicles')
        flash('Vehicle deleted successfully', 'success')
    else:
        flash('Vehicle not found', 'error')

    return redirect(url_for('dealer.inventory'))


@dealer_bp.route('/inventory/<int:car_id>')
@dealer_required
@kyc_required
def inventory_detail(car_id):
    dealer_id = get_dealer_id()
    vehicle = vehicle_get(car_id)

    if not vehicle or vehicle['dealer_id'] != dealer_id:
        flash('Vehicle not found', 'error')
        return redirect(url_for('dealer.inventory'))

    # FIXED: load extra gallery images from vehicle_images table and attach to vehicle dict
    # Without this, vehicle.get('extra_images') in the template is always None,
    # so the gallery never shows uploaded photos even after they are saved to DB.
    try:
        extra_imgs = (
            VehicleImage.query
            .filter_by(vehicle_id=car_id)
            .order_by(VehicleImage.sort_order, VehicleImage.id)
            .all()
        )
        vehicle['extra_images'] = [img.filename for img in extra_imgs]
        # Pass full image objects so template can show type labels
        vehicle['extra_images_typed'] = [
            {'filename': img.filename, 'image_type': img.image_type, 'id': img.id}
            for img in extra_imgs
        ]
    except Exception as e:
        current_app.logger.error(
            f"Could not load extra images for vehicle {car_id}: {e}")
        vehicle['extra_images'] = []
        vehicle['extra_images_typed'] = []

    # v29.2: attach profit summary and expenses
    profit = vehicle_profit_summary(car_id)
    expenses = expenses_get_by_vehicle(car_id)
    from models import InventoryExpense
    expense_types = InventoryExpense.EXPENSE_TYPES

    return render_template('dealer/inventory_detail.html',
                           vehicle=vehicle,
                           profit=profit,
                           expenses=expenses,
                           expense_types=expense_types)


# ========== INVENTORY EXPENSE API (v29.2) ==========

@dealer_bp.route('/inventory/<int:car_id>/expenses', methods=['POST'])
@dealer_required
@kyc_required
def add_expense(car_id):
    dealer_id = get_dealer_id()
    vehicle = vehicle_get(car_id)
    if not vehicle or vehicle['dealer_id'] != dealer_id:
        return jsonify({'error': 'Vehicle not found or unauthorized'}), 403

    amount_str = request.form.get('amount', '').strip()
    expense_type = request.form.get('expense_type', '').strip()
    expense_date = request.form.get('expense_date', '').strip()
    note = request.form.get('note', '').strip()

    if not expense_type or not expense_date:
        return jsonify({'error': 'expense_type and expense_date are required'}), 400
    try:
        amount = float(amount_str)
        if amount < 0:
            raise ValueError
    except (ValueError, TypeError):
        return jsonify({'error': 'Amount must be a non-negative number'}), 400

    exp_id = expense_create({
        'vehicle_id':   car_id,
        'dealer_id':    dealer_id,
        'expense_type': expense_type,
        'amount':       amount,
        'expense_date': expense_date,
        'note':         note,
    })
    log_dealer_action(f'Added expense ({expense_type} ₹{amount}) to vehicle ID:{car_id}', 'Inventory')
    return jsonify({'success': True, 'expense_id': exp_id,
                    'profit': vehicle_profit_summary(car_id)}), 201


@dealer_bp.route('/inventory/<int:car_id>/expenses/<int:exp_id>', methods=['POST'])
@dealer_required
@kyc_required
def edit_expense(car_id, exp_id):
    dealer_id = get_dealer_id()
    vehicle = vehicle_get(car_id)
    if not vehicle or vehicle['dealer_id'] != dealer_id:
        return jsonify({'error': 'Unauthorized'}), 403

    from models import InventoryExpense
    exp = InventoryExpense.query.get(exp_id)
    if not exp or exp.vehicle_id != car_id or exp.dealer_id != dealer_id:
        return jsonify({'error': 'Expense not found or unauthorized'}), 403

    amount_str = request.form.get('amount', '').strip()
    expense_type = request.form.get('expense_type', '').strip()
    expense_date = request.form.get('expense_date', '').strip()
    note = request.form.get('note', '').strip()

    if not expense_type or not expense_date:
        return jsonify({'error': 'expense_type and expense_date are required'}), 400
    try:
        amount = float(amount_str)
        if amount < 0:
            raise ValueError
    except (ValueError, TypeError):
        return jsonify({'error': 'Amount must be a non-negative number'}), 400

    expense_update(exp_id, {
        'expense_type': expense_type,
        'amount':       amount,
        'expense_date': expense_date,
        'note':         note,
    })
    log_dealer_action(f'Updated expense ID:{exp_id} on vehicle ID:{car_id}', 'Inventory')
    return jsonify({'success': True, 'profit': vehicle_profit_summary(car_id)})


@dealer_bp.route('/inventory/<int:car_id>/expenses/<int:exp_id>/delete', methods=['POST'])
@dealer_required
@kyc_required
def delete_expense(car_id, exp_id):
    dealer_id = get_dealer_id()
    vehicle = vehicle_get(car_id)
    if not vehicle or vehicle['dealer_id'] != dealer_id:
        return jsonify({'error': 'Unauthorized'}), 403

    from models import InventoryExpense
    exp = InventoryExpense.query.get(exp_id)
    if not exp or exp.vehicle_id != car_id or exp.dealer_id != dealer_id:
        return jsonify({'error': 'Expense not found or unauthorized'}), 403

    expense_delete(exp_id)
    log_dealer_action(f'Deleted expense ID:{exp_id} from vehicle ID:{car_id}', 'Inventory')
    return jsonify({'success': True, 'profit': vehicle_profit_summary(car_id)})


@dealer_bp.route('/inventory/<int:car_id>/profit', methods=['GET'])
@dealer_required
@kyc_required
def get_vehicle_profit(car_id):
    dealer_id = get_dealer_id()
    vehicle = vehicle_get(car_id)
    if not vehicle or vehicle['dealer_id'] != dealer_id:
        return jsonify({'error': 'Unauthorized'}), 403
    return jsonify(vehicle_profit_summary(car_id))


@dealer_bp.route('/inventory/upload-images', methods=['POST'])
@dealer_required
@kyc_required
def upload_vehicle_images():
    """
    AJAX endpoint — accepts multiple image files and stores them as
    extra gallery images for a vehicle.

    Request (multipart/form-data):
        vehicle_id  int
        images      file[]   (up to 15 files per batch; jpg/jpeg/png/gif/webp)

    Response (JSON):
        { "success": true,  "filenames": ["abc.jpg", ...], "count": N }
        { "success": false, "error": "..." }
    """
    dealer_id = get_dealer_id()
    vehicle_id = request.form.get('vehicle_id', type=int)

    if not vehicle_id:
        return jsonify({'success': False, 'error': 'vehicle_id required'}), 400

    vehicle = vehicle_get(vehicle_id)
    if not vehicle or vehicle['dealer_id'] != dealer_id:
        return jsonify({'success': False, 'error': 'Vehicle not found'}), 404

    files = request.files.getlist('images')
    # Support per-file image_type: client can send image_types[] matching order of images[]
    # Falls back to 'gallery' for all files if not provided
    image_types = request.form.getlist('image_types')
    saved = []
    MAX_FILES = 15
    ALLOWED = {'jpg', 'jpeg', 'png', 'webp'}  # webp accepted then converted

    # FIXED: entire file-save + DB block wrapped in try/except.
    # Previously, any filesystem or DB error would bubble up to Flask's default
    # error handler which returns an HTML 500 page. The browser JS then tried
    # to parse that HTML as JSON → "Unexpected token '<'" crash.
    # Now: errors always return {"success": false, "error": "..."} JSON + rollback.
    try:
        for idx, f in enumerate(files[:MAX_FILES]):
            if not f or not f.filename:
                continue
            if not is_allowed_image(f.filename):
                continue

            img_type = image_types[idx] if idx < len(image_types) else 'gallery'
            # Validate image_type value
            valid_types = VehicleImage.MANDATORY_TYPES + ['gallery']
            if img_type not in valid_types:
                img_type = 'gallery'

            # For typed mandatory slots: replace existing record if one exists
            if img_type != 'gallery':
                existing = VehicleImage.query.filter_by(
                    vehicle_id=vehicle_id, image_type=img_type).first()
                if existing:
                    # Remove old file from disk
                    old_path = os.path.join(
                        current_app.config['UPLOAD_FOLDER'], existing.filename)
                    if os.path.exists(old_path):
                        try:
                            os.remove(old_path)
                        except OSError:
                            pass
                    db.session.delete(existing)

            fname = save_uploaded_image(
                f, current_app.config['UPLOAD_FOLDER'], uuid.uuid4().hex)

            sort_val = VehicleImage.MANDATORY_TYPES.index(img_type) \
                if img_type in VehicleImage.MANDATORY_TYPES else 10 + len(saved)

            img_rec = VehicleImage(
                vehicle_id=vehicle_id,
                filename=fname,
                image_type=img_type,
                sort_order=sort_val
            )
            db.session.add(img_rec)
            saved.append({'filename': fname, 'image_type': img_type})

        if saved:
            db.session.commit()

        filenames = [s['filename'] for s in saved]
        return jsonify({'success': True, 'filenames': filenames,
                        'saved': saved, 'count': len(saved)})

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(
            f"upload_vehicle_images error for vehicle {vehicle_id}: {e}")
        return jsonify({'success': False, 'error': 'Upload failed. Please try again.'}), 500

@dealer_bp.route('/inventory/delete-image', methods=['POST'])
@dealer_required
@kyc_required
def delete_vehicle_image():
    """AJAX endpoint — delete a single gallery image by filename."""
    dealer_id = get_dealer_id()
    vehicle_id = request.form.get('vehicle_id', type=int)
    filename = request.form.get('filename', '').strip()

    if not vehicle_id or not filename:
        return jsonify({'success': False, 'error': 'vehicle_id and filename required'}), 400

    vehicle = vehicle_get(vehicle_id)
    if not vehicle or vehicle['dealer_id'] != dealer_id:
        return jsonify({'success': False, 'error': 'Not found'}), 404

    try:
        img = VehicleImage.query.filter_by(vehicle_id=vehicle_id, filename=filename).first()
        if img:
            db.session.delete(img)
            db.session.commit()
        # Remove file from disk
        file_path = os.path.join(current_app.config['UPLOAD_FOLDER'], filename)
        if os.path.exists(file_path):
            os.remove(file_path)
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500


@dealer_bp.route('/inventory/replace-image', methods=['POST'])
@dealer_required
@kyc_required
def replace_vehicle_image():
    """
    AJAX endpoint — replace a specific VehicleImage record with a new upload.
    Overwrites the file on disk keeping the same filename (so DB stays clean),
    OR creates a new file and updates the DB record if replace_vi_id is given.

    Request (multipart/form-data):
        vehicle_id   int
        images       file   (single image)
        image_types  str    (the image_type e.g. 'front', 'interior', 'gallery')
        replace_vi_id int   (VehicleImage.id to replace)
    """
    dealer_id  = get_dealer_id()
    vehicle_id = request.form.get('vehicle_id', type=int)
    vi_id      = request.form.get('replace_vi_id', type=int)

    if not vehicle_id:
        return jsonify({'success': False, 'error': 'vehicle_id required'}), 400

    vehicle = vehicle_get(vehicle_id)
    if not vehicle or vehicle['dealer_id'] != dealer_id:
        return jsonify({'success': False, 'error': 'Vehicle not found'}), 404

    files = request.files.getlist('images')
    if not files or not files[0].filename:
        return jsonify({'success': False, 'error': 'No image provided'}), 400

    f = files[0]
    if not is_allowed_image(f.filename):
        return jsonify({'success': False, 'error': 'Invalid image type'}), 400

    img_type = (request.form.get('image_types') or 'gallery').strip()
    valid_types = VehicleImage.MANDATORY_TYPES + ['gallery']
    if img_type not in valid_types:
        img_type = 'gallery'

    try:
        # If we have a specific vi_id, overwrite that record's file
        if vi_id:
            vi = VehicleImage.query.filter_by(id=vi_id, vehicle_id=vehicle_id).first()
            if vi:
                # Save new file, delete old
                new_fname = save_uploaded_image(
                    f, current_app.config['UPLOAD_FOLDER'], uuid.uuid4().hex)
                old_path = os.path.join(current_app.config['UPLOAD_FOLDER'], vi.filename)
                if os.path.exists(old_path):
                    try:
                        os.remove(old_path)
                    except OSError:
                        pass
                vi.filename  = new_fname
                vi.image_type = img_type
                db.session.commit()

                # If this was the primary image, update vehicle record too
                if vehicle.get('image_filename') == vi.filename:
                    from db import execute
                    execute("UPDATE vehicles SET image_filename=%s WHERE id=%s",
                            (new_fname, vehicle_id))

                return jsonify({'success': True, 'filename': new_fname, 'vi_id': vi_id})

        # Fallback: treat as a normal typed upload (replace by type)
        existing = VehicleImage.query.filter_by(
            vehicle_id=vehicle_id, image_type=img_type).first()
        if existing:
            old_path = os.path.join(current_app.config['UPLOAD_FOLDER'], existing.filename)
            if os.path.exists(old_path):
                try:
                    os.remove(old_path)
                except OSError:
                    pass
            db.session.delete(existing)

        fname = save_uploaded_image(
            f, current_app.config['UPLOAD_FOLDER'], uuid.uuid4().hex)
        sort_val = VehicleImage.MANDATORY_TYPES.index(img_type) \
            if img_type in VehicleImage.MANDATORY_TYPES else 99
        img_rec = VehicleImage(
            vehicle_id=vehicle_id, filename=fname,
            image_type=img_type, sort_order=sort_val)
        db.session.add(img_rec)
        db.session.commit()
        return jsonify({'success': True, 'filename': fname})

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"replace_vehicle_image error: {e}")
        return jsonify({'success': False, 'error': 'Replace failed. Please try again.'}), 500


@dealer_bp.route('/inventory/set-primary-image', methods=['POST'])
@dealer_required
@kyc_required
def set_primary_image():
    """
    AJAX endpoint — set a VehicleImage as the primary image for a vehicle.
    Called automatically after mask editor saves a processed image.

    Request (JSON): { vi_id: int, vehicle_id: int }
    """
    dealer_id = get_dealer_id()
    data      = request.get_json(silent=True) or {}
    vi_id     = data.get('vi_id')
    vehicle_id = data.get('vehicle_id')

    if not vi_id or not vehicle_id:
        return jsonify({'success': False, 'error': 'vi_id and vehicle_id required'}), 400

    vehicle = vehicle_get(vehicle_id)
    if not vehicle or vehicle['dealer_id'] != dealer_id:
        return jsonify({'success': False, 'error': 'Vehicle not found'}), 404

    vi = VehicleImage.query.filter_by(id=int(vi_id), vehicle_id=vehicle_id).first()
    if not vi:
        return jsonify({'success': False, 'error': 'Image not found'}), 404

    try:
        from db import vehicle_update
        vehicle_update(vehicle_id, {'image_filename': vi.filename})
        return jsonify({'success': True, 'filename': vi.filename})
    except Exception as e:
        current_app.logger.error(f'set_primary_image error: {e}')
        return jsonify({'success': False, 'error': 'Could not update primary'}), 500


# ========== LEADS ==========


@dealer_bp.route('/leads')
@dealer_required
@kyc_required
def leads():
    dealer_id = get_dealer_id()
    stage = request.args.get('stage', '')
    search = request.args.get('search', '')
    page = request.args.get('page', 1, type=int)

    leads_data = leads_get_by_dealer(
        dealer_id, stage=stage, search=search, page=page)
    stage_counts = leads_get_stage_counts(dealer_id)

    return render_template('dealer/leads.html',
                           leads=leads_data['items'],
                           stage_counts=stage_counts,
                           stage_filter=stage,
                           search=search,
                           current_page=leads_data['page'],
                           total_pages=leads_data['pages'],
                           total=leads_data['total'],
                           stages=['new', 'interested',
                                   'test_drive', 'negotiation', 'lost',
                                   'connected', 'not_connected']
                           )


@dealer_bp.route('/leads/add', methods=['GET', 'POST'])
@dealer_required
@kyc_required
def add_lead():
    import re
    dealer_id = get_dealer_id()
    agents = agents_get_by_dealer(dealer_id)
    vehicles = vehicles_get_by_dealer(dealer_id, per_page=100)['items']

    if request.method == 'POST':
        follow_up_date = request.form.get('follow_up_date')
        stage = request.form.get('stage')

        # Backend validation: connected stage requires follow_up_date
        if stage == 'connected' and not follow_up_date:
            flash('Follow-up Date is required when Stage is "Connected"', 'error')
            return render_template('dealer/lead_form.html', lead=None, agents=agents, vehicles=vehicles)

        # Only connected keeps follow_up_date; all others → NULL
        if stage != 'connected':
            follow_up_date = None

        # Validate follow-up date (must be future)
        if follow_up_date:
            from datetime import datetime
            follow_dt = datetime.strptime(follow_up_date, '%Y-%m-%dT%H:%M')
            if follow_dt.date() <= datetime.now().date():
                flash(
                    'Follow-up date must be a future date (tomorrow or later)', 'error')
                return render_template('dealer/lead_form.html', lead=None, agents=agents, vehicles=vehicles)

        # Phone validation: 10-digit number only
        customer_phone = ''.join(c for c in (request.form.get('customer_phone') or '') if c.isdigit())[:10]
        phone_valid = len(customer_phone) == 10 if customer_phone else True
        if customer_phone and not phone_valid:
            flash('Phone must be a valid 10-digit number.', 'error')
            return render_template('dealer/lead_form.html', lead=None, agents=agents, vehicles=vehicles)

        lead_data = {
            'dealer_id': dealer_id,
            'agent_id': request.form.get('agent_id') or None,
            'vehicle_id': request.form.get('vehicle_id') or None,
            'customer_name': request.form.get('customer_name'),
            'customer_email': request.form.get('customer_email'),
            'customer_phone': customer_phone,
            'customer_city': request.form.get('customer_city'),
            'source': request.form.get('source'),
            'stage': request.form.get('stage'),
            'notes': request.form.get('notes'),
            'budget': float(request.form.get('budget')) if request.form.get('budget') else None,
            'follow_up_date': follow_up_date
        }

        lead_create(lead_data)
        log_dealer_action(f'Created lead for {lead_data.get("customer_name", "customer")}', 'Leads')
        flash('Lead added successfully', 'success')
        return redirect(url_for('dealer.leads'))

    return render_template('dealer/lead_form.html', lead=None, agents=agents, vehicles=vehicles)


@dealer_bp.route('/leads/edit/<int:lid>', methods=['GET', 'POST'])
@dealer_required
@kyc_required
def edit_lead(lid):
    dealer_id = get_dealer_id()
    lead = lead_get(lid)

    if not lead or lead['dealer_id'] != dealer_id:
        flash('Lead not found', 'error')
        return redirect(url_for('dealer.leads'))

    agents = agents_get_by_dealer(dealer_id)
    vehicles = vehicles_get_by_dealer(dealer_id, per_page=100)['items']

    if request.method == 'POST':
        follow_up_date = request.form.get('follow_up_date')
        stage = request.form.get('stage')

        # Backend validation: connected stage requires follow_up_date
        if stage == 'connected' and not follow_up_date:
            flash('Follow-up Date is required when Stage is "Connected"', 'error')
            return render_template('dealer/lead_form.html', lead=lead, agents=agents, vehicles=vehicles)

        # Only connected keeps follow_up_date; all others → NULL
        if stage != 'connected':
            follow_up_date = None

        # Validate follow-up date (must be future)
        if follow_up_date:
            from datetime import datetime
            follow_dt = datetime.strptime(follow_up_date, '%Y-%m-%dT%H:%M')
            if follow_dt.date() <= datetime.now().date():
                flash(
                    'Follow-up date must be a future date (tomorrow or later)', 'error')
                return render_template('dealer/lead_form.html', lead=lead, agents=agents, vehicles=vehicles)

        # Phone validation: +91XXXXXXXXXX or 10-digit number
        import re
        customer_phone = (request.form.get('customer_phone') or '').strip()
        phone_valid = bool(
            re.match(r'^\+91[0-9]{10}$', customer_phone) or
            re.match(r'^[0-9]{10}$', customer_phone)
        )
        if not phone_valid:
            flash('Phone must be a 10-digit number or +91 followed by 10 digits.', 'error')
            return render_template('dealer/lead_form.html', lead=lead, agents=agents, vehicles=vehicles)

        update_data = {
            'agent_id': request.form.get('agent_id') or None,
            'vehicle_id': request.form.get('vehicle_id') or None,
            'customer_name': request.form.get('customer_name'),
            'customer_email': request.form.get('customer_email'),
            'customer_phone': customer_phone,
            'customer_city': request.form.get('customer_city'),
            'source': request.form.get('source'),
            'stage': request.form.get('stage'),
            'notes': request.form.get('notes'),
            'budget': float(request.form.get('budget')) if request.form.get('budget') else None,
            'follow_up_date': follow_up_date
        }

        lead_update(lid, update_data)
        log_dealer_action(f'Updated lead for {update_data.get("customer_name", "customer")} (ID:{lid})', 'Leads')
        flash('Lead updated successfully', 'success')
        return redirect(url_for('dealer.leads'))

    return render_template('dealer/lead_form.html', lead=lead, agents=agents, vehicles=vehicles)


@dealer_bp.route('/leads/delete/<int:lid>', methods=['POST'])
@dealer_required
@kyc_required
def delete_lead(lid):
    dealer_id = get_dealer_id()
    lead = lead_get(lid)

    if lead and lead['dealer_id'] == dealer_id:
        lead_delete(lid)
        flash('Lead deleted successfully', 'success')
    else:
        flash('Lead not found', 'error')

    return redirect(url_for('dealer.leads'))

# ========== AGENTS ==========


@dealer_bp.route('/agents')
@dealer_required
@kyc_required
def agents():
    dealer_id = get_dealer_id()
    agents_list = agents_get_by_dealer(dealer_id)
    agent_leads_count = agents_get_leads_count(dealer_id)

    return render_template('dealer/agents.html',
                           agents=agents_list,
                           agent_leads_count=agent_leads_count
                           ,
                           quota=check_quota(dealer_id, current_plan_key(), 'agents', adding=0))


@dealer_bp.route('/agents/add', methods=['POST'])
@dealer_required
@kyc_required
@quota_required('agents')
def add_agent():
    import re
    dealer_id = get_dealer_id()
    phone = (request.form.get('phone') or '').strip()

    # Validate phone: 10-digit number only
    phone_digits = ''.join(c for c in phone if c.isdigit())[:10]
    phone_valid = len(phone_digits) == 10
    if not phone_valid:
        flash('Phone must be a valid 10-digit number.', 'error')
        return redirect(url_for('dealer.agents'))

    agent_data = {
        'dealer_id': dealer_id,
        'name': request.form.get('name'),
        'email': request.form.get('email'),
        'phone': phone_digits,
        # Status is no longer on the Add form — a new agent starts Available and
        # is changed from the table row or the Edit modal. Passing None here
        # would override the model default and store NULL.
        'status': request.form.get('status') or 'available'
    }

    agent_create(agent_data)
    log_dealer_action(f'Added agent: {agent_data.get("name")}', 'Agents')
    flash('Agent added successfully', 'success')
    return redirect(url_for('dealer.agents'))


@dealer_bp.route('/agents/edit/<int:agent_id>', methods=['POST'])
@dealer_required
@kyc_required
def edit_agent(agent_id):
    dealer_id = get_dealer_id()
    agent = agent_get(agent_id)

    if agent and agent['dealer_id'] == dealer_id:
        phone = (request.form.get('phone') or '').strip()
        phone_digits = ''.join(c for c in phone if c.isdigit())[:10]
        if len(phone_digits) != 10:
            flash('Phone must be a valid 10-digit number.', 'error')
            return redirect(url_for('dealer.agents'))

        update_data = {
            'name': request.form.get('name'),
            'email': request.form.get('email'),
            'phone': phone_digits,
            'status': request.form.get('status')
        }
        agent_update(agent_id, update_data)
        log_dealer_action(f'Updated agent: {update_data.get("name")} (ID:{agent_id})', 'Agents')
        flash('Agent updated successfully', 'success')
    else:
        flash('Agent not found', 'error')

    return redirect(url_for('dealer.agents'))


@dealer_bp.route('/agents/update-status/<int:agent_id>', methods=['POST'])
@dealer_required
@kyc_required
def update_agent_status(agent_id):
    dealer_id = get_dealer_id()
    agent = agent_get(agent_id)

    if agent and agent['dealer_id'] == dealer_id:
        new_status = request.form.get('status')
        agent_update(agent_id, {'status': new_status})
        log_dealer_action(f'Set agent {agent_id} status to {new_status}', 'Agents')
        flash('Agent status updated', 'success')

    return redirect(url_for('dealer.agents'))


@dealer_bp.route('/agents/delete/<int:agent_id>', methods=['POST'])
@dealer_required
@kyc_required
def delete_agent(agent_id):
    dealer_id = get_dealer_id()
    agent = agent_get(agent_id)

    if agent and agent['dealer_id'] == dealer_id:
        agent_name = agent.get('name', f'ID:{agent_id}')
        agent_delete(agent_id)
        log_dealer_action(f'Deleted agent: {agent_name}', 'Agents')
        flash('Agent deleted successfully', 'success')
    else:
        flash('Agent not found', 'error')

    return redirect(url_for('dealer.agents'))

# ========== DEALS ==========


@dealer_bp.route('/deals')
@dealer_required
@kyc_required
def deals():
    dealer_id = get_dealer_id()
    status = request.args.get('status', '')

    deals_list = deals_get_by_dealer(dealer_id, status=status)
    status_counts = deals_get_status_counts(dealer_id)

    return render_template('dealer/deals.html',
                           deals=deals_list,
                           status_counts=status_counts,
                           status_filter=status,
                           statuses=['negotiation', 'booked',
                                     'finalized', 'delivered', 'cancelled']
                           )


@dealer_bp.route('/deals/add', methods=['GET', 'POST'])
@dealer_required
@kyc_required
def add_deal():
    import re
    dealer_id = get_dealer_id()
    # A sold car cannot be sold again, so keep it out of the picker.
    # edit_deal deliberately does NOT do this — an existing deal must still
    # show the vehicle it is attached to, even after that car is marked sold.
    vehicles = vehicles_get_by_dealer(dealer_id, per_page=100, exclude_sold=True)['items']

    if request.method == 'POST':
        # Phone validation: 10-digit number only (optional in deal)
        customer_phone = ''.join(c for c in (request.form.get('customer_phone') or '') if c.isdigit())[:10]
        if customer_phone and len(customer_phone) != 10:
            flash('Phone must be a valid 10-digit number.', 'error')
            return render_template('dealer/deal_form.html', deal=None, vehicles=vehicles)

        # Final price validation
        final_price_raw = request.form.get('final_price')
        if not final_price_raw or float(final_price_raw) <= 0:
            flash('Final Price is required and must be greater than 0.', 'error')
            return render_template('dealer/deal_form.html', deal=None, vehicles=vehicles)

        deal_data = {
            'dealer_id': dealer_id,
            'lead_id': request.form.get('lead_id') or None,
            'vehicle_id': int(request.form.get('vehicle_id')),
            'customer_name': request.form.get('customer_name'),
            'customer_phone': customer_phone,
            'customer_email': request.form.get('customer_email'),
            'asking_price': float(request.form.get('asking_price')) if request.form.get('asking_price') else None,
            'final_price': float(final_price_raw),
            # Payment Details was removed from the deal form. The columns stay —
            # deals, invoice, finance and reports all still read them — so a new
            # deal simply records cash with no financing attached.
            'payment_mode': request.form.get('payment_mode') or 'cash',
            'loan_amount': float(request.form.get('loan_amount')) if request.form.get('loan_amount') else None,
            'down_payment': float(request.form.get('down_payment')) if request.form.get('down_payment') else None,
            'emi_months': int(request.form.get('emi_months')) if request.form.get('emi_months') else None,
            'emi_amount': float(request.form.get('emi_amount')) if request.form.get('emi_amount') else None,
            'bank_name': request.form.get('bank_name'),
            'status': request.form.get('status'),
            'booking_amount': float(request.form.get('booking_amount')) if request.form.get('booking_amount') else 0,
            'notes': request.form.get('notes')
        }

        deal_id = deal_create(deal_data)
        log_dealer_action(f'Created deal for {deal_data.get("customer_name", "customer")} — ₹{deal_data.get("final_price", 0):,.0f}', 'Sales')
        flash('Deal created successfully', 'success')
        return redirect(url_for('dealer.deals'))

    return render_template('dealer/deal_form.html', deal=None, vehicles=vehicles)


@dealer_bp.route('/deals/edit/<int:did_>', methods=['GET', 'POST'])
@dealer_required
@kyc_required
def edit_deal(did_):
    dealer_id = get_dealer_id()
    deal = deal_get(did_)

    if not deal or deal['dealer_id'] != dealer_id:
        flash('Deal not found', 'error')
        return redirect(url_for('dealer.deals'))

    vehicles = vehicles_get_by_dealer(dealer_id, per_page=100)['items']

    if request.method == 'POST':
        import re
        # Phone validation: 10-digit number only (optional in deal)
        customer_phone = ''.join(c for c in (request.form.get('customer_phone') or '') if c.isdigit())[:10]
        if customer_phone and len(customer_phone) != 10:
            flash('Phone must be a valid 10-digit number.', 'error')
            return render_template('dealer/deal_form.html', deal=deal, vehicles=vehicles)

        # Final price validation
        final_price_raw = request.form.get('final_price')
        if not final_price_raw or float(final_price_raw) <= 0:
            flash('Final Price is required and must be greater than 0.', 'error')
            return render_template('dealer/deal_form.html', deal=deal, vehicles=vehicles)

        update_data = {
            'customer_name': request.form.get('customer_name'),
            'customer_phone': customer_phone,
            'customer_email': request.form.get('customer_email'),
            'asking_price': float(request.form.get('asking_price')) if request.form.get('asking_price') else None,
            'final_price': float(final_price_raw),
            # These no longer post, so fall back to what is stored. Without this,
            # opening an existing financed deal and saving would erase its bank,
            # loan amount and EMI figures.
            'payment_mode': request.form.get('payment_mode') or deal.get('payment_mode') or 'cash',
            'loan_amount': (float(request.form.get('loan_amount'))
                            if request.form.get('loan_amount') else deal.get('loan_amount')),
            'down_payment': (float(request.form.get('down_payment'))
                             if request.form.get('down_payment') else deal.get('down_payment')),
            'emi_months': (int(request.form.get('emi_months'))
                           if request.form.get('emi_months') else deal.get('emi_months')),
            'emi_amount': (float(request.form.get('emi_amount'))
                           if request.form.get('emi_amount') else deal.get('emi_amount')),
            'bank_name': request.form.get('bank_name') or deal.get('bank_name'),
            'status': request.form.get('status'),
            'booking_amount': float(request.form.get('booking_amount')) if request.form.get('booking_amount') else 0,
            'notes': request.form.get('notes')
        }

        deal_update(did_, update_data)
        log_dealer_action(f'Updated deal ID:{did_} for {update_data.get("customer_name", "customer")}', 'Sales')
        flash('Deal updated successfully', 'success')
        return redirect(url_for('dealer.deals'))

    return render_template('dealer/deal_form.html', deal=deal, vehicles=vehicles)


@dealer_bp.route('/deals/invoice/<int:did_>')
@dealer_required
@kyc_required
def invoice(did_):
    dealer_id = get_dealer_id()
    deal = deal_get(did_)

    if not deal or deal['dealer_id'] != dealer_id:
        flash('Deal not found', 'error')
        return redirect(url_for('dealer.deals'))

    vehicle = deal.get('vehicle')
    dealer_info = g.user

    return render_template('dealer/invoice.html', deal=deal, vehicle=vehicle, dealer=dealer_info)

# ========== FINANCE ==========


@dealer_bp.route('/finance')
@dealer_required
@kyc_required
@feature_required('finance')
def finance():
    dealer_id = get_dealer_id()
    financial = deals_get_financial_summary(dealer_id)
    recent_deals = deals_get_by_dealer(dealer_id)[:10]

    return render_template('dealer/finance.html',
                           total_revenue=financial['total_revenue'],
                           total_deals=financial['total_deals'],
                           loan_deals=financial['loan_deals'],
                           cash_deals=financial['cash_deals'],
                           total_gst=financial['total_gst'],
                           recent_deals=recent_deals
                           )

# ========== DOCUMENTS ==========


@dealer_bp.route('/documents')
@dealer_required
@kyc_required
def documents():
    dealer_id = get_dealer_id()
    # Own uploaded documents (dealer's Documents table)
    docs = documents_get_by_dealer(dealer_id)
    vehicles = vehicles_get_by_dealer(dealer_id, per_page=100)['items']
    # CDS documents assigned to this dealer by admin
    try:
        from db import cds_dealer_active_docs
        cds_docs = cds_dealer_active_docs(dealer_id)
    except Exception:
        cds_docs = []

    return render_template('dealer/documents.html', documents=docs, vehicles=vehicles, cds_docs=cds_docs)


@dealer_bp.route('/documents/upload', methods=['POST'])
@dealer_required
@kyc_required
def upload_document():
    dealer_id = get_dealer_id()

    file = request.files.get('document')
    if not file or not file.filename:
        flash('Please select a file', 'error')
        return redirect(url_for('dealer.documents'))

    ext = file.filename.rsplit('.', 1)[-1].lower()
    if ext not in ['pdf', 'jpg', 'jpeg', 'png']:
        flash('Invalid file type. Allowed: PDF, JPG, PNG', 'error')
        return redirect(url_for('dealer.documents'))

    filename = f"{uuid.uuid4().hex}.{ext}"
    file.save(os.path.join(current_app.config['UPLOAD_FOLDER'], filename))

    doc_data = {
        'dealer_id': dealer_id,
        'vehicle_id': request.form.get('vehicle_id') or None,
        'customer_name': request.form.get('customer_name'),
        'doc_type': request.form.get('doc_type'),
        'filename': filename,
        'original_name': file.filename,
        # Notes was removed from the form; the column stays for older records.
        'notes': None
    }

    document_create(doc_data)

    # ── Register in Centralized Document Storage ──────────────────────────────
    try:
        from db import cds_register
        cds_register({
            'dealer_id':     dealer_id,
            'file_name':     filename,
            'original_name': file.filename,
            'file_path':     os.path.join('images', 'uploads', filename),
            'module_name':   'Documents',
            'document_type': request.form.get('doc_type', ''),
            'uploaded_by':   dealer_id,
            'performed_by':  f'dealer:{dealer_id}',
            'user_role':     'Dealer',
        })
    except Exception:
        pass  # Never break the main upload flow
    # ─────────────────────────────────────────────────────────────────────────

    flash('Document uploaded successfully', 'success')
    log_dealer_action(f'Uploaded document: {file.filename}', 'Documents')
    return redirect(url_for('dealer.documents'))


@dealer_bp.route('/documents/edit/<int:docid>', methods=['POST'])
@dealer_required
@kyc_required
def edit_document(docid):
    """
    Update a document's type, owner or linked vehicle, and optionally swap the
    file. Leaving the file input empty keeps the existing one — replacing it
    writes the new file first and only deletes the old one once that succeeded,
    so a failed upload never leaves the record pointing at nothing.
    """
    dealer_id = get_dealer_id()
    docs = documents_get_by_dealer(dealer_id)
    doc = next((d for d in docs if d['id'] == docid), None)
    if not doc:
        flash('Document not found', 'error')
        return redirect(url_for('dealer.documents'))

    from models import Document
    record = Document.query.filter_by(id=docid, dealer_id=dealer_id).first()
    if not record:
        flash('Document not found', 'error')
        return redirect(url_for('dealer.documents'))

    doc_type = (request.form.get('doc_type') or '').strip()
    if not doc_type:
        flash('Document type is required', 'error')
        return redirect(url_for('dealer.documents'))

    record.doc_type = doc_type
    record.customer_name = (request.form.get('customer_name') or '').strip()
    record.vehicle_id = request.form.get('vehicle_id') or None

    file = request.files.get('document')
    old_filename = record.filename
    replaced = False

    if file and file.filename:
        ext = file.filename.rsplit('.', 1)[-1].lower()
        if ext not in ['pdf', 'jpg', 'jpeg', 'png']:
            flash('Invalid file type. Allowed: PDF, JPG, PNG', 'error')
            return redirect(url_for('dealer.documents'))
        new_name = f"{uuid.uuid4().hex}.{ext}"
        file.save(os.path.join(current_app.config['UPLOAD_FOLDER'], new_name))
        record.filename = new_name
        record.original_name = file.filename
        replaced = True

    try:
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        current_app.logger.warning('document edit failed: %s', exc)
        flash('Could not save the changes', 'error')
        return redirect(url_for('dealer.documents'))

    # only now is the old file safe to remove
    if replaced and old_filename:
        old_path = os.path.join(current_app.config['UPLOAD_FOLDER'], old_filename)
        if os.path.exists(old_path):
            try:
                os.remove(old_path)
            except OSError:
                pass

    log_dealer_action(f'Edited document: {record.original_name or record.filename}', 'Documents')
    flash('Document updated' + (' and file replaced' if replaced else ''), 'success')
    return redirect(url_for('dealer.documents'))


@dealer_bp.route('/documents/delete/<int:docid>', methods=['POST'])
@dealer_required
@kyc_required
def delete_document(docid):
    dealer_id = get_dealer_id()
    docs = documents_get_by_dealer(dealer_id)
    doc = next((d for d in docs if d['id'] == docid), None)

    if doc:
        # Delete file
        filepath = os.path.join(
            current_app.config['UPLOAD_FOLDER'], doc['filename'])
        if os.path.exists(filepath):
            os.remove(filepath)
        document_delete(docid)
        log_dealer_action(f'Deleted document: {doc.get("original_name", doc.get("filename"))}', 'Documents')
        flash('Document deleted successfully', 'success')
    else:
        flash('Document not found', 'error')

    return redirect(url_for('dealer.documents'))

# ========== REPORTS ==========


@dealer_bp.route('/reports')
@dealer_required
@kyc_required
@feature_required('reports')
def reports():
    dealer_id = get_dealer_id()

    # Day / Week / Month / Year / All Time. Calendar-aligned, not rolling.
    from utils.report_period import PERIODS, resolve, filter_rows, describe
    period, start, end, period_label = resolve(request.args.get('period'))

    # Leads are filtered in SQL; deals are already fetched as dicts, so they
    # are filtered in Python rather than adding a second query.
    lead_stages = leads_get_stage_counts(dealer_id, since=start, until=end)
    total_leads = sum(lead_stages.values())
    converted = lead_stages.get('converted', 0)
    conversion_rate = round(
        (converted / total_leads * 100) if total_leads > 0 else 0)

    sources = leads_get_source_counts(dealer_id, since=start, until=end)
    all_deals = filter_rows(deals_get_by_dealer(dealer_id), start, end)

    # Current stock and the 6-month revenue trend are point-in-time views, not
    # period sums, so the filter deliberately does not touch them.
    inventory_summary = vehicles_inventory_summary(dealer_id)
    fuels = vehicles_get_fuel_breakdown(dealer_id)
    monthly = deals_get_monthly_revenue(dealer_id)

    return render_template('dealer/reports.html',
                           total_revenue=sum(
                               d['final_price'] for d in all_deals if d['status'] == 'delivered'),
                           total_inventory=inventory_summary['total'],
                           available_count=inventory_summary['available'],
                           sold_count=inventory_summary['sold'],
                           conversion_rate=conversion_rate,
                           converted=converted,
                           total_leads=total_leads,
                           all_deals=all_deals,
                           sources=sources,
                           fuels=fuels,
                           monthly=monthly,
                           periods=PERIODS,
                           period=period,
                           period_label=period_label,
                           period_range=describe(period, start, end)
                           )

# ========== INQUIRIES ==========


@dealer_bp.route('/inquiries')
@dealer_required
@kyc_required
def inquiries():
    dealer_id = get_dealer_id()
    inquiries_list = inquiries_get_by_dealer(dealer_id)

    return render_template('dealer/inquiries.html', inquiries=inquiries_list)


@dealer_bp.route('/inquiries/respond/<int:iid>', methods=['POST'])
@dealer_required
@kyc_required
def respond_inquiry(iid):
    dealer_id = get_dealer_id()
    inquiries_list = inquiries_get_by_dealer(dealer_id)
    inquiry = next((i for i in inquiries_list if i['id'] == iid), None)

    if inquiry:
        inquiry_update_status(iid, 'responded')
        log_dealer_action(f'Responded to inquiry #{iid}', 'Inquiries')
        flash('Inquiry marked as responded', 'success')
    else:
        flash('Inquiry not found', 'error')

    return redirect(url_for('dealer.inquiries'))

# ========== SUBSCRIPTION ==========


# ── Plan definitions (single source of truth) ─────────────────────────────
PLANS = [
    {
        'name': 'Starter', 'key': 'starter', 'price_inr': 0, 'period': 'month',
        'recommended': False,
        'features': [
            'Dealer Dashboard', 'Inventory Management — 10 Cars', 'Car Listings',
            'Lead Management — Basic', 'CRM — Basic', '1 Staff / Agent',
            'Deals & Sales — Basic', 'Document Management — Basic',
            'Reports & Analytics — Basic', 'Caryanams Marketing',
            'Car Studio — 5/month', 'Dealer Website — Basic',
            'Challan Check — 2/month', 'RTO Check — 2/month', 'Car History Check — 2/month'
        ]
    },
    {
        'name': 'Growth', 'key': 'growth', 'price_inr': 999, 'period': 'month',
        'recommended': True,
        'features': [
            'Dealer Dashboard', 'Inventory Management — 100 Cars', 'Car Listings',
            'Lead Management', 'CRM', '3 Staff / Agents', 'Follow-up & Reminders',
            'Deals & Sales', 'Finance Management', 'Document Management',
            'Reports & Analytics', 'Caryanams Marketing', 'Car Studio — 100/month',
            'Image Enhancement', 'Dealer Website', 'Challan Check — 20/month',
            'RTO Check — 20/month', 'Car History Check — 20/month',
            'AI Price Suggestion', 'Stock Aging Analysis', 'Inventory Profitability',
            'Priority Support'
        ]
    },
    {
        'name': 'Pro', 'key': 'pro', 'price_inr': 1999, 'period': 'month',
        'recommended': False,
        'features': [
            'Dealer Dashboard', 'Inventory Management — Unlimited', 'Car Listings',
            'Lead Management — Advanced', 'CRM', '10 Staff / Agents',
            'Follow-up & Reminders', 'Deals & Sales', 'Finance Management',
            'Document Management', 'Reports & Analytics — Advanced',
            'Caryanams Marketing', 'Car Studio — 500/month', 'Image Enhancement',
            'Dealer Website — Advanced (Mini Website)', 'Challan Check — 100/month',
            'RTO Check — 100/month', 'Car History Check — 100/month',
            'AI Price Suggestion — Advanced', 'Stock Aging Analysis',
            'Inventory Profitability — Advanced', 'Dead Stock Alerts',
            'Market Price Intelligence', "Buy / Don't Buy Recommendation",
            'Dealer-to-Dealer', 'Priority Support'
        ]
    },
]

def _get_razorpay_client():
    import razorpay
    return razorpay.Client(auth=(
        current_app.config['RAZORPAY_KEY_ID'],
        current_app.config['RAZORPAY_KEY_SECRET']
    ))


def _generate_demo_transaction_id():
    """Generate a transaction id like DEMO-20260001 (year + running sequence)."""
    from models import DealerPayment
    year = _now_ist().strftime('%Y')
    count_this_year = DealerPayment.query.filter(
        DealerPayment.transaction_id.like(f'DEMO-{year}%')
    ).count()
    return f'DEMO-{year}{str(count_this_year + 1).zfill(4)}'


@dealer_bp.route('/subscription')
@dealer_required
def subscription():
    """
    Select Plan page — step 4 of the reference flow.

    Reached automatically by the portal when a dealer has no active plan
    (`?onboarding=1`), and manually from the sidebar for upgrades/renewals.
    All state is read from the database on every request, so it is correct
    on a new device, in a new browser and after any logout.
    """
    dealer_id = get_dealer_id()

    state       = subscription_state()
    current_sub = state['subscription'] or get_active_subscription(dealer_id)
    last_sub    = get_latest_subscription(dealer_id)

    # Onboarding mode = "this dealer cannot leave until a plan is active".
    onboarding = (request.args.get('onboarding') == '1') or (not state['has_plan'])

    return render_template(
        'dealer/subscription.html',
        plans=PLANS,
        dealer=g.user,
        current_sub=current_sub,
        last_sub=last_sub,
        sub_state=state,
        onboarding=onboarding,
        has_active_plan=state['has_plan'],
        current_plan_key=(state['plan'] or ''),
        razorpay_enabled=current_app.config.get('RAZORPAY_ENABLED', False),
        allow_free_plan=current_app.config.get('ALLOW_FREE_PLAN_ACTIVATION', True),
    )


@dealer_bp.route('/subscription/status')
@dealer_required
def subscription_status():
    """JSON snapshot of the caller's plan — used by the plan page and for debugging."""
    state = subscription_state()
    expires = state['expires_at']
    return jsonify({
        'success': True,
        'has_active_plan': state['has_plan'],
        'plan': state['plan'],
        'payment_status': state['status'],
        'source': state['source'],
        'expires_at': expires.isoformat() if expires else None,
        'days_left': state['days_left'],
        'dashboard_url': url_for('dealer.dashboard'),
    })


# ── Plan Activation (AJAX) — demo payment or free activation ──────────────
# Flipping RAZORPAY_ENABLED=True later only requires creating the order here
# and calling activate_plan_for_dealer() from the signature-verify route:
# the activation writer, the gate and the redirect all stay exactly as they are.
@dealer_bp.route('/subscription/activate', methods=['POST'])
@dealer_required
def activate_subscription():
    data     = request.get_json(silent=True) or {}
    plan_key = (data.get('plan') or '').strip().lower()
    method   = (data.get('method') or 'demo').strip().lower()      # 'demo' | 'free'

    plan = next((p for p in PLANS if p['key'] == plan_key), None)
    if not plan:
        return jsonify({'success': False, 'error': 'Invalid plan selected.'}), 400

    dealer_id = get_dealer_id()

    # v29 FIX: g.user is a dict (see db.user_get_by_id), so the old
    # `g.user.name` raised AttributeError → 500 → the browser fell back to a
    # localStorage-only "activation" that never reached the database. That is
    # why a plan vanished after logging out and back in.
    dealer_name = (g.user.get('name', '') if isinstance(g.user, dict)
                   else getattr(g.user, 'name', '')) or ''

    if current_app.config.get('RAZORPAY_ENABLED', False):
        # ── Live Razorpay: create an order and let the browser check out.
        # Verify the signature in a separate route, then call
        # activate_plan_for_dealer(...) with payment_status='Paid'.
        return jsonify({
            'success': False,
            'error': 'Online payments are not enabled yet. Please contact support.',
        }), 400

    # A zero-price plan is free by definition — it must always be activatable,
    # otherwise a new dealer has no way to finish onboarding. v28 rejected
    # 'starter' outright, which left new accounts with no subscription row at all.
    if int(plan['price_inr']) == 0:
        method = 'free'

    if method == 'free':
        if int(plan['price_inr']) > 0 and not current_app.config.get('ALLOW_FREE_PLAN_ACTIVATION', True):
            return jsonify({
                'success': False,
                'error': 'Free activation of paid plans is currently disabled by admin.',
            }), 403
        amount, payment_method = 0, 'Free'
        payment_status, transaction_id = 'Free Trial', None
        success_msg = f'{plan["name"]} plan activated. Welcome to Caryanams DMS!'
    else:
        amount, payment_method = int(plan['price_inr']), 'Demo'
        # v29: written as 'Paid' (was 'Pending'), so "did this dealer pay?"
        # has an unambiguous answer. Legacy 'Pending' demo rows still work.
        payment_status = 'Paid'
        transaction_id = _generate_demo_transaction_id()
        success_msg = 'Payment successful. Your subscription is now active.'

    sub = activate_plan_for_dealer(
        dealer_id, plan['key'],
        price=amount,
        payment_method=payment_method,
        payment_status=payment_status,
        transaction_id=transaction_id,
        days=int(current_app.config.get('SUBSCRIPTION_DEFAULT_DAYS', 30)),
        dealer_name=dealer_name,
        notes=f'{dealer_name} activated {plan["name"]} plan via {payment_method} flow.',
    )

    log_dealer_action(
        f'Activated subscription plan: {plan["name"]} via {payment_method}'
        + (f' (Txn: {transaction_id})' if transaction_id else ''),
        'Subscription'
    )

    return jsonify({
        'success':        True,
        'plan':           plan['key'],
        'plan_name':      plan['name'],
        'payment_method': payment_method,
        'payment_status': payment_status,
        'amount':         amount,
        'transaction_id': transaction_id,
        'expires_at':     sub.expires_at.isoformat() if sub.expires_at else None,
        # The dashboard is unlocked the moment the row above is committed.
        'redirect_url':   url_for('dealer.dashboard'),
        'message':        success_msg,
    })

# ========== MY ACCOUNT ==========


@dealer_bp.route('/my-account', methods=['GET', 'POST'])
@dealer_required
def my_account():
    """
    Profile + login mobile number. Dealers sign in with an SMS code sent to
    this number, so changing it requires a code sent to the NEW number first
    (a typo would otherwise lock the dealer out). There is no password.
    """
    from models import db, User
    from utils.otp import normalise_phone, mask_phone, phone_in_use, issue_otp, verify_otp, seconds_until_resend
    from utils.sms import is_test_mode
    dealer_id = get_dealer_id()
    pending = session.get('phone_change')
    if pending and pending.get('user_id') != dealer_id:
        session.pop('phone_change', None)
        pending = None

    if request.method == 'POST':
        action = request.form.get('action')
        user = User.query.get(dealer_id)

        if action == 'update_profile' and user:
            name = request.form.get('name', '').strip()
            if name:
                user.name = name
                db.session.commit()
                session['user_name'] = user.name
                log_dealer_action('Updated profile information', 'Profile')
                flash('Profile updated successfully!', 'success')
            else:
                flash('Name cannot be empty.', 'error')

        elif action == 'phone_send' and user:
            new_phone = normalise_phone(request.form.get('new_phone'))
            if not new_phone:
                flash('Please enter a valid 10-digit mobile number.', 'error')
            elif new_phone == normalise_phone(user.phone):
                flash('That is already your login number.', 'info')
            elif phone_in_use(new_phone, exclude_user_id=user.id):
                flash('This mobile number is already used by another account.', 'error')
            else:
                ok, msg, wait = issue_otp(new_phone, 'change_phone')
                flash(msg, 'success' if ok else 'error')
                if ok or wait:
                    session['phone_change'] = {'user_id': user.id, 'phone': new_phone}

        elif action == 'phone_resend' and pending:
            ok, msg, _ = issue_otp(pending['phone'], 'change_phone')
            flash(msg, 'success' if ok else 'error')

        elif action == 'phone_verify' and pending and user:
            ok, msg = verify_otp(pending['phone'], 'change_phone', request.form.get('code'))
            if not ok:
                flash(msg, 'error')
            elif phone_in_use(pending['phone'], exclude_user_id=user.id):
                session.pop('phone_change', None)
                flash('This mobile number is already used by another account.', 'error')
            else:
                old = user.phone
                user.phone = '+91' + pending['phone']
                db.session.commit()
                session.pop('phone_change', None)
                log_dealer_action(f'Changed login mobile number (…{(old or "")[-4:]} → …{pending["phone"][-4:]})', 'Profile')
                flash(f'Login mobile number changed to {mask_phone(pending["phone"])}. Use it next time you sign in.', 'success')

        elif action == 'phone_cancel':
            session.pop('phone_change', None)

        return redirect(url_for('dealer.my_account'))

    pending = session.get('phone_change')
    return render_template(
        'dealer/my_account.html', current_user=g.user,
        phone_pending_masked=mask_phone(pending['phone']) if pending else '',
        phone_resend_wait=seconds_until_resend(pending['phone'], 'change_phone') if pending else 0,
        login_phone_masked=mask_phone(normalise_phone((g.user or {}).get('phone') if isinstance(g.user, dict) else getattr(g.user, 'phone', ''))),
        otp_test_mode=is_test_mode(),
    )


# ========== WEBSITE SETTINGS ==========


@dealer_bp.route('/website-settings', methods=['GET', 'POST'])
@dealer_required
@kyc_required
@feature_required('mini_website')
def website_settings():
    """Dealer can set their website_name, upload website_logo, and fill contact info."""
    dealer_id = get_dealer_id()
    dealer_user = User.query.get(dealer_id)

    if request.method == 'POST':
        # ── Website name: sanitise to a clean slug ─────────────────────────
        raw_name = request.form.get('website_name', '').strip()
        slug_error = None

        if raw_name:
            import re
            # Keep only alphanumerics, hyphens, underscores; convert spaces → hyphens
            slug = re.sub(r'[^a-zA-Z0-9_-]', '-', raw_name.strip())
            slug = re.sub(r'-{2,}', '-', slug).strip('-')   # collapse multiple hyphens
            slug = slug[:80]                                  # max length

            if not slug:
                slug_error = 'Website name can only contain letters, numbers, hyphens and underscores.'
            else:
                # Check for duplicate (exclude current dealer)
                existing = User.query.filter(
                    User.role == 'dealer',
                    User.website_name == slug,
                    User.id != dealer_id
                ).first()
                if existing:
                    slug_error = f'The name "{slug}" is already taken by another dealer. Please choose a different name.'
                else:
                    dealer_user.website_name = slug

        if slug_error:
            flash(slug_error, 'error')
            # Re-render with error (don't save anything)
            return render_template('dealer/website_settings.html', dealer=dealer_user)

        # ── Optional logo upload ─────────────────────────────────────────
        logo_file = request.files.get('website_logo')
        if logo_file and logo_file.filename and is_allowed_image(logo_file.filename):
            logo_fname = save_uploaded_image(
                logo_file, current_app.config['UPLOAD_FOLDER'],
                f"wlogo_{dealer_id}_{uuid.uuid4().hex[:8]}",
                vehicle_mode=False
            )
            dealer_user.website_logo = logo_fname

        # ── Other fields ─────────────────────────────────────────────────
        dealer_user.whatsapp_number = request.form.get(
            'whatsapp_number', '').strip() or dealer_user.whatsapp_number
        dealer_user.address = request.form.get('address', '').strip()
        dealer_user.google_maps_url = request.form.get(
            'google_maps_url', '').strip()
        dealer_user.years_in_business = request.form.get(
            'years_in_business', type=int) or dealer_user.years_in_business
        dealer_user.business_hours = request.form.get(
            'business_hours', '').strip()

        db.session.commit()
        log_dealer_action('Updated website settings', 'Website')
        flash('Website settings saved!', 'success')
        return redirect(url_for('dealer.website_settings'))

    return render_template('dealer/website_settings.html', dealer=dealer_user)
