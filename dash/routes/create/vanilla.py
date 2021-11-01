import base64
import random
import re
import secrets
import string
from email.utils import parseaddr
from io import BytesIO

import aiohttp
from argon2 import PasswordHasher
import i18n
from PIL import Image
from sanic import Blueprint, response
from sendgrid import Mail, SendGridAPIClient
from stegano import lsb

from dash import app, env
from dash.crypto import Crypto
from dash.data import db
from dash.data.item import PenguinItem
from dash.data.mail import PenguinPostcard
from dash.data.penguin import ActivationKey, Penguin

vanilla_create = Blueprint('vanilla_create', url_prefix='/create/vanilla')
passh = PasswordHasher()

all_captchas = [
    ('balloon', Image.open('./dash/templates/images/balloon.png')),
    ('cheese', Image.open('./dash/templates/images/cheese.png')),
    ('igloo', Image.open('./dash/templates/images/igloo.png')),
    ('pizza', Image.open('./dash/templates/images/pizza.png')),
    ('popcorn', Image.open('./dash/templates/images/popcorn.png')),
    ('watermelon', Image.open('./dash/templates/images/watermelon.png'))
]


@vanilla_create.get('/<lang:(en|fr|pt|es)>')
async def create_page(request, lang):
    base64_captchas = []
    captchas = random.sample(all_captchas, min(len(all_captchas), 3))
    captcha_answer = random.choice(captchas)[0]
    captcha_object = [captcha for captcha in captchas if captcha_answer in captcha]

    if 'anon_token' not in request.ctx.session:
        anon_token = secrets.token_urlsafe(32)
        request.ctx.session['anon_token'] = anon_token

    request.ctx.session['captcha_answer'] = captchas.index(captcha_object[0])
    request.ctx.session['captcha'] = {
        'passed': 0
    }
    request.ctx.session['errors'] = {
        'name': True,
        'pass': True,
        'email': True,
        'terms': True,
        'captcha': True
    }

    request.ctx.session['captcha_answer'] = captchas.index(captcha_object[0])
    
    for captcha_image in captchas:
        captcha_encoded = lsb.hide(captcha_image[1].copy(), request.ctx.session['anon_token'])
        buffered = BytesIO()
        captcha_encoded.save(buffered, format='PNG')
        captcha_base64 = base64.b64encode(buffered.getvalue())
        base64_captchas.append(captcha_base64.decode('utf-8'))

    register_template = env.get_template(f'create/{lang}.html')
    page = register_template.render(
        VANILLA_PLAY_LINK=app.config.VANILLA_PLAY_LINK,
        anon_token=request.ctx.session['anon_token'],
        captcha_1=base64_captchas[0],
        captcha_2=base64_captchas[1],
        captcha_3=base64_captchas[2],
        captcha_answer=i18n.t(f'create.{captcha_answer}', locale=lang),
        site_key=app.config.GSITE_KEY
    )
    return response.html(page)


@vanilla_create.post('/<lang:(en|fr|pt|es)>')
async def register(request, lang):
    trigger = request.form.get('_triggering_element_name', None)
    anon_token = request.form.get('anon_token', None)
    if 'anon_token' not in request.ctx.session:
        return response.json(
            {
                'message': '403 Forbidden'
            },
            status=403
        )
    elif not anon_token or request.ctx.session['anon_token'] != anon_token:
        return response.json(
            {
                'message': '403 Forbidden'
            },
            status=403
        )
    elif trigger == 'name':
        return await _validate_username(request, lang)
    elif trigger == 'pass':
        return _validate_password(request, lang)
    elif trigger == 'email':
        return await _validate_email(request, lang)
    elif trigger == 'terms':
        return _validate_terms(request, lang)
    elif trigger == 'captcha':
        return _validate_captcha(request, lang)
    return await _validate_registration(request, lang)
    

async def _validate_registration(request, lang):
    username = request.form.get('name', None)
    password = request.form.get('pass', None)
    email = request.form.get('email', None)
    color = request.form.get('color', None)
    if 'username' not in request.ctx.session or request.ctx.session['username'] != username:
        return response.json(
            {
                'message': '403 Forbidden'
            },
            status=403
        )
    elif 'password' not in request.ctx.session or request.ctx.session['password'] != password:
        return response.json(
            {
                'message': '403 Forbidden'
            },
            status=403
        )
    elif 'email' not in request.ctx.session or request.ctx.session['email'] != email:
        return response.json(
            {
                'message': '403 Forbidden'
            },
            status=403
        )
    elif not color.isdigit() or int(color) not in range(1, 17):
        return response.json(
            {
                'message': '403 Forbidden'
            },
            status=403
        )
    elif app.config.GSECRET_KEY:
        gclient_response = request.form.get('recaptcha_response', None)
        async with aiohttp.ClientSession() as session:
            async with session.post(app.config.GCAPTCHA_URL, data=dict(
                secret=app.config.GSECRET_KEY,
                response=gclient_response,
                remoteip=request.ip
            )) as resp:
                gresult = await resp.json()
                if not gresult['success']:
                    return response.text('Your captcha score was low, please try again.')
    password = Crypto.hash(password).upper()
    password = Crypto.get_login_hash(password, rndk=app.config.STATIC_KEY)
    password = passh.hash(password)

    username = username.strip()

    if app.config.USERNAME_FORCE_CASE:
        username = username.title()

    penguin = await Penguin.create(username=username.lower(), nickname=username, password=password, email=email,
                                   color=int(color),
                                   approval_en=app.config.APPROVE_USERNAME,
                                   approval_pt=app.config.APPROVE_USERNAME,
                                   approval_fr=app.config.APPROVE_USERNAME,
                                   approval_es=app.config.APPROVE_USERNAME,
                                   approval_de=app.config.APPROVE_USERNAME,
                                   approval_ru=app.config.APPROVE_USERNAME,
                                   active=app.config.ACTIVATE_PLAYER)

    await PenguinItem.create(penguin_id=penguin.id, item_id=int(color))
    await PenguinPostcard.create(penguin_id=penguin.id, sender_id=None, postcard_id=125)

    if not app.config.ACTIVATE_PLAYER:
        activation_key = secrets.token_urlsafe(45)
        mail_template = env.get_template(f'emails/activation/vanilla/{lang}.html')
        message = Mail(
            from_email=app.config.FROM_EMAIL, to_emails=email,
            subject=i18n.t('activate.mail_subject', locale=lang),
            html_content=mail_template.render(
                penguin=penguin, site_name=app.config.SITE_NAME,
                activation_code=activation_key,
                VANILLA_PLAY_LINK=app.config.VANILLA_PLAY_LINK,
                activate_link=f'{app.config.VANILLA_PLAY_LINK}/{lang}/penguin/activate'
            )
        )
        sg = SendGridAPIClient(app.config.SENDGRID_API_KEY)
        sg.send(message)
        await ActivationKey.create(penguin_id=penguin.id, activation_key=activation_key)
    return response.redirect(app.config.VANILLA_PLAY_LINK)


async def _validate_username(request, lang):
    username = request.form.get('name', None)
    if not username:
        request.ctx.session['errors']['name'] = True
        return response.json(
            [
                _make_error_message('name', i18n.t('create.name_missing', locale=lang)), 
                _remove_class('name', 'valid'),
                _add_class('name', 'error'),
                _update_errors(request.ctx.session['errors'])
            ],
            headers={
                'X-Drupal-Ajax-Token': 1
            }
        )

    username = username.strip()
    if len(username) < 4 or len(username) > 12:
        request.ctx.session['errors']['name'] = True
        return response.json(
            [
                _make_error_message('name', i18n.t('create.name_short', locale=lang)), 
                _remove_class('name', 'valid'),
                _add_class('name', 'error'),
                _update_errors(request.ctx.session['errors'])
            ],
            headers={
                'X-Drupal-Ajax-Token': 1
            }
        )
    elif len(re.sub('[^0-9]', '', username)) > 5:
        request.ctx.session['errors']['name'] = True
        return response.json(
            [
                _make_error_message('name', i18n.t('create.name_number', locale=lang)), 
                _remove_class('name', 'valid'),
                _add_class('name', 'error'),
                _update_errors(request.ctx.session['errors'])
            ],
            headers={
                'X-Drupal-Ajax-Token': 1
            }
        )
    elif re.search('[a-zA-Z]', username) is None:
        request.ctx.session['errors']['name'] = True
        return response.json(
            [
                _make_error_message('name', i18n.t('create.name_letter', locale=lang)), 
                _remove_class('name', 'valid'),
                _add_class('name', 'error'),
                _update_errors(request.ctx.session['errors'])
            ],
            headers={
                'X-Drupal-Ajax-Token': 1
            }
        )
    elif not all(letter.isalnum() or letter.isspace() for letter in username):
        request.ctx.session['errors']['name'] = True
        return response.json(
            [
                _make_error_message('name', i18n.t('create.name_not_allowed', locale=lang)), 
                _remove_class('name', 'valid'),
                _add_class('name', 'error'),
                _update_errors(request.ctx.session['errors'])
            ],
            headers={
                'X-Drupal-Ajax-Token': 1
            }
        )
    nickname = username.rstrip(string.digits)
    names = await db.select([Penguin.username]).where(Penguin.username.like(f'{nickname.lower()}%')).gino.all()
    names = {name for name, in names}
    if username.lower() in names:
        request.ctx.session['errors']['name'] = True
        max_digits = min(5, 12 - len(nickname))
        usernames_gen = (f'{nickname}{i}' for i in range(1, int('9' * max_digits)) if f'{nickname.lower()}{i}' not in names)
        usernames = [next(usernames_gen) for _ in range(3)]
        if usernames is None:
            return response.json(
                [
                    _make_error_message('name', i18n.t('create.name_taken', locale=lang)), 
                    _remove_class('name', 'valid'),
                    _add_class('name', 'error'),
                    _update_errors(request.ctx.session['errors'])
                ],
                headers={
                    'X-Drupal-Ajax-Token': 1
                }
            )
        return response.json(
                [
                    _make_name_suggestion(usernames, i18n.t('create.vanilla_name_suggest', locale=lang)),
                    _remove_class('name', 'valid'),
                    _add_class('name', 'error'),
                    _update_errors(request.ctx.session['errors'])
                ],
                headers={
                    'X-Drupal-Ajax-Token': 1
                }
            )
    request.ctx.session['errors']['name'] = False
    request.ctx.session['username'] = username
    return response.json(
        [ 
            _remove_class('name', 'error'),
            _add_class('name', 'valid'),
            _update_errors(request.ctx.session['errors'])
        ],
        headers={
            'X-Drupal-Ajax-Token': 1
        }
    )


def _validate_password(request, lang):
    password = request.form.get('pass', None)
    if not password:
        request.ctx.session['errors']['pass'] = True
        return response.json( 
            [
                _make_error_message('pass', i18n.t('create.password_short', locale=lang)), 
                _remove_class('pass', 'valid'),
                _add_class('pass', 'error'),
                _update_errors(request.ctx.session['errors'])
            ],
            headers={
                'X-Drupal-Ajax-Token': 1
            }
        )
    elif len(password) < 4:
        request.ctx.session['errors']['pass'] = True
        return response.json(
            [
                _make_error_message('pass', i18n.t('create.password_short', locale=lang)), 
                _remove_class('pass', 'valid'),
                _add_class('pass', 'error'),
                _update_errors(request.ctx.session['errors'])
            ],
            headers={
                'X-Drupal-Ajax-Token': 1
            }
        )
    request.ctx.session['errors']['pass'] = False
    request.ctx.session['password'] = password
    return response.json(
        [
            _remove_class('pass', 'error'),
            _add_class('pass', 'valid'),
            _update_errors(request.ctx.session['errors'])
        ],
        headers={
            'X-Drupal-Ajax-Token': 1
        }
    )


async def _validate_email(request, lang):
    email = request.form.get('email', None)
    _, email = parseaddr(email)
    domain = email.rsplit('@', 1)[-1]
    if not email or '@' not in email:
        request.ctx.session['errors']['email'] = True
        return response.json(
            [
                _make_error_message('email', i18n.t('create.email_invalid', locale=lang)), 
                _remove_class('email', 'valid'),
                _add_class('email', 'error'),
                _update_errors(request.ctx.session['errors'])
            ],
            headers={
                'X-Drupal-Ajax-Token': 1
            }
        )
    elif app.config.EMAIL_WHITELIST and domain not in app.config.EMAIL_WHITELIST:
        request.ctx.session['errors']['email'] = True
        return response.json(
            [
                _make_error_message('email', i18n.t('create.email_invalid', locale=lang)), 
                _remove_class('email', 'valid'),
                _add_class('email', 'error'),
                _update_errors(request.ctx.session['errors'])
            ],
            headers={
                'X-Drupal-Ajax-Token': 1
            }
        )

    email_count = await db.select([db.func.count(Penguin.email)]).where(
        db.func.lower(Penguin.email) == email.lower()).gino.scalar()

    if email_count >= app.config.MAX_ACCOUNT_EMAIL:
        request.ctx.session['errors']['email'] = True
        return response.json( 
            [
                _make_error_message('email', i18n.t('create.email_invalid', locale=lang)), 
                _remove_class('email', 'valid'),
                _add_class('email', 'error'),
                _update_errors(request.ctx.session['errors'])
            ],
            headers={
                'X-Drupal-Ajax-Token': 1
            }
        )
    request.ctx.session['errors']['email'] = False
    request.ctx.session['email'] = email
    return response.json(
        [
            _remove_class('email', 'error'),
            _add_class('email', 'valid'),
            _update_errors(request.ctx.session['errors'])
        ],
        headers={
            'X-Drupal-Ajax-Token': 1
        }
    )


def _validate_terms(request, lang):
    terms = request.form.get('terms', None)
    if not terms:
        request.ctx.session['errors']['terms'] = True
        return response.json(
            [
                _make_error_message('terms', i18n.t('create.terms', locale=lang)),
                _remove_class('terms', 'valid'),
                _add_class('terms', 'error'),
                _update_errors(request.ctx.session['errors'])
            ],
            headers={
                'X-Drupal-Ajax-Token': 1
            }
        )
    request.ctx.session['errors']['terms'] = False
    return response.json(
        [
            _add_class('terms', 'checked'),
            _update_errors(request.ctx.session['errors'])
        ],
        headers={
            'X-Drupal-Ajax-Token': 1
        }
    )


def _validate_captcha(request, lang):
    captcha_answer = request.form.get('captcha', None)
    if 'captcha_answer' not in request.ctx.session:
        return response.json(
            {
                'message': '403 Forbidden'
            },
            status=403
        )
    elif int(captcha_answer) == int(request.ctx.session['captcha_answer']):
        request.ctx.session['errors']['captcha'] = False
        request.ctx.session['captcha']['passed'] = 1
        return response.json(
            [
                _update_captcha(request.ctx.session['captcha']['passed']),
                _update_errors(request.ctx.session['errors'])
            ],
            headers={
                'X-Drupal-Ajax-Token': 1
            }
        )
    return response.json(
        [
            _make_error_message('captcha', i18n.t('create.captcha_invalid', locale=lang)),
        ],
        headers={
            'X-Drupal-Ajax-Token': 1
        }
    )


def _update_errors(new_setting):
    return (
        {
            'command': 'settings',
            'merge': True,
            'settings': {
                'penguin': {
                    'errors': new_setting
                }
            }
        }
    )


def _update_captcha(new_setting):
    return (
        {
            'command': 'settings',
            'merge': True,
            'settings': {
                'penguin': {
                    'captcha': {
                        'passed': new_setting
                    }
                }
            }
        }
    )


def _make_name_suggestion(names, message):
    name_suggestion_template = env.get_template('html/name_suggestion.html')
    return (
        {
            'command': 'insert',
            'selector': '#name-error',
            'method': 'html',
            'data': name_suggestion_template.render(
                names=names,
                message=message
            )
        }
    )


def _make_error_message(name, message):
    error_template = env.get_template('html/error.html')
    return (
        {
            'command': 'insert',
            'selector': f'#{name}-error',
            'method': 'html',
            'data': error_template.render(
                message=message
            )
        }
    )


def _add_class(name, arguments):
    return (
        {
            'command': 'invoke',
            'selector': f'#edit-{name}',
            'method': 'addClass',
            'arguments': [arguments]
        }
    )


def _remove_class(name, arguments):
    return (
        {
            'command': 'invoke',
            'selector': f'#edit-{name}',
            'method': 'removeClass',
            'arguments': [arguments]
        }
    )
