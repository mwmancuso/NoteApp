"""Models defining all authentication related operations.

Models:
    Users: handles all users; admins have separate manager.
    Methods: login methods for users.
    Tokens: handles tokens for various authentication matters.

Each model has related functions and managers that may be called to
operate on select models. If repetitive or lengthy functions are
required elsewhere, they should be defined here. Constants from this
file should also be used wherever possible.
"""

import base64
from django.db import models
import onetimepass as otp
import pytz
import smtplib
from voluptuous import Schema, All, Required, Match, MultipleInvalid
import bcrypt
from django.core.mail import send_mail
from django.conf import settings
import hashlib
import datetime
import logging

import meta.models
from errors import validators
from errors.exceptions import UserError
from authentication.helpers import random_string

logger = logging.getLogger(__name__)

# Pseudo-function to trick makemessages into making message files
_ = lambda s: s

# Static variables for clarity in database
USER_STANDARD = 0
USER_ADMIN = 1
METHOD_PASSWORD = 0
METHOD_VALIDATION_TOKEN = 1
METHOD_RECOVERY_TOKEN = 2
METHOD_OATH_KEY = 3
METHOD_ACTIVE = 1
METHOD_INACTIVE = 0
TOKEN_NEW_USER = 'new-user'

# Constants for use in authentication related script
SALT_ROUNDS = 13 # Number of Bcrypt salt rounds for encryption
INVALID_LOGIN = _('invalid-login') # Defined to provide ambiguous response
INVALID_RECOVERY = _('invalid-recovery')
TOKEN_SALT_SIZE = 64 # Token generator length
TOKEN_SIZE = 20
TOKEN_TIME = datetime.timedelta(days=30)
VALIDATION_TIME = datetime.timedelta(hours=5)
OATH_STRING_SIZE = 10 # Must be > 10, and multiples of 5 for no =s

class UserManager(models.Manager):
    """Manager for the Users model.

    Exists to provide table-level operations such as creating users and
    logging them in.

    The key difference for manager vs. model functions is that manager
    functions create Users instances, whereas model functions operate
    on existing ones.

    Note that this manager only allows operation on standard users.
    Other managers allow for operation on other specific user-types.
    """

    def user_exists(self, username):
        """Checks to see if user exists.

        Args:
            username: username to test, can be any case.

        Returns:
            Bool; true if exists, false if not.
        """

        try:
            self.get_queryset().get(username__iexact=username)
        except Users.DoesNotExist:
            return False

        return True

    def email_exists(self, email):
        """Checks to see if email exists.

        Args:
            email: username to test, can be any case.

        Returns:
            Bool; true if exists, false if not.
        """

        try:
            self.get_queryset().get(email__iexact=email)
        except Users.DoesNotExist:
            return False

        return True

    def create(self, **user_info):
        """Creates user based on defined variables.

        Handles validation of credentials and creates database entry
        if validation succeeds. Does not create admin entries; admin
        entries are to be created by a different function to prevent
        accidental admin granting. Additional validation must also be
        employed. Also assigns user to instance if successful.

        Following are the info variables that may need to be
        defined prior to calling create. *s mean the variable is
        required:
            username*
            first_name
            last_name
            email*
            password* (plaintext)
            token - required if new-user setting is set to token

        Args:
            user_info: see above.

        Returns:
            The user object if successful.

        Raises:
            UserError: describes error.
        """

        user_errors = []
        token_required = False
        token_object = None
        validated = dict()

        # Check to see if user creation is enabled
        new_users = meta.models.Data.objects.get(tag='new-users')

        if new_users.setting == 0:
            if new_users.data == 'token':
                token_required = True
            else:
                user_errors.append(_('creation-disabled'))
                raise UserError(*user_errors)

        # Voluptuous schema for validation; tested with try statement
        schema_dict = {
            Required('username', _('username-required')): All(str,
                Match(validators.VALID_USERNAME_REGEX),
                msg=_('invalid-username')),
            'first_name': All(str, Match(validators.VALID_NAME_REGEX),
                msg=_('invalid-first-name')),
            'last_name': All(str, Match(validators.VALID_NAME_REGEX),
                msg=_('invalid-last-name')),
            Required('email', _('email-required')): All(str,
                Match(validators.VALID_EMAIL_REGEX),
                msg=_('invalid-email')),
            Required('password', _('password-required')): All(str,
                validators.Password, msg=_('invalid-password')),
        }

        # Adds required key to schema if token is required
        if token_required:
            schema_dict[Required('token', _('token-required'))] = All(str,
                Match(validators.VALID_TOKEN_REGEX), msg=_('invalid-token'))

        schema = Schema(schema_dict)

        try:
            validated = schema(user_info)

            # Deletes user_info to get rid of sensitive data
            del user_info
        except MultipleInvalid as error:
            user_errors = validators.list_errors(error)

            raise UserError(*user_errors)

        current_user = self.get_queryset().filter(
            username__iexact=validated['username'])

        if current_user:
            user_errors.append(_('user-exists'))

        current_email = self.get_queryset().filter(email__iexact=validated[
            'email'])

        if current_email:
            user_errors.append(_('email-exists'))

        if user_errors:
            raise UserError(*user_errors)

        # Checks to see if token is in database if required
        if token_required:
            try:
                token_object = Tokens.objects.get(
                    purpose=TOKEN_NEW_USER, token=validated['token'])
            except Tokens.DoesNotExist:
                user_errors.append(_('no-such-token'))
                raise UserError(*user_errors)

            if token_object.exhausted:
                user_errors.append(_('token-exhausted'))

            if token_object.expired():
                user_errors.append(_('token-expired'))

            if user_errors:
                raise UserError(*user_errors)

            token_object.exhausted = True

            # Prevents entry of token into User object
            del validated['token']

        # Hashes password using bcrypt
        encrypted_password = bcrypt.hashpw(
            validated['password'].encode('utf-8'), bcrypt.gensalt(
            SALT_ROUNDS))

        # Deletes password from info so it doesn't get inserted on creation
        del validated['password']

        # Creates password method
        password_method = Methods()
        password_method.method = METHOD_PASSWORD
        password_method.password = encrypted_password
        password_method.step = 1

        # Creates validation token
        random_salt = random_string(size=TOKEN_SALT_SIZE)
        email = validated['email']
        token = hashlib.sha1((email + random_salt).encode('utf-8')).hexdigest()

        validation_token_method = Methods()
        validation_token_method.method = METHOD_VALIDATION_TOKEN
        validation_token_method.token = token
        validation_token_method.step = 0

        # Saves all data if validation was complete
        if token_required:
            token_object.save()

        user_object = self.get_queryset().create(**validated)

        password_method.user = user_object
        password_method.save()
        validation_token_method.user = user_object
        validation_token_method.save()

        # Emails user; may use template for email in future
        subject = 'Account Validation'
        text = 'Your validation token is:\n%s' % token

        try:
            send_mail(subject, text, settings.EMAIL_HOST_USER, [email])
        except smtplib.SMTPException as error:
            logger.exception(error)

            user_errors.append(_('validation-email-failure'))
            raise UserError(*user_errors)

        return user_object

    def login_password(self, update_access=True, **user_info):
        """Handler to login via password authentication.

        Accepts two pieces of information: username and password. Both
        are required. Validates, checks and returns user object if user
        exists and password is correct.

        Args:
            update_access: boolean, updates database access time.
            user_info: see above.

        Returns:
            User object if successful.

        Raises:
            UserError: contains description.
        """

        user_errors = []

        # Check to see if user login is enabled
        user_login = meta.models.Data.objects.get(tag='user-login')

        if user_login.setting == 0:
            user_errors.append(_('login-disabled'))
            raise UserError(*user_errors)

        # Voluptuous schema for validation; tested with try statement
        schema = Schema({
            Required('username', _('username-required')): All(str,
                msg=_('invalid-username')),
            Required('password', _('password-required')): All(str,
                msg=_('invalid-password')),
        })

        try:
            validated = schema(user_info)

            # Deletes user_info to get rid of sensitive data
            del user_info
        except MultipleInvalid as error:
            user_errors = validators.list_errors(error)
            raise UserError(*user_errors)

        try:
            user_object = self.get_queryset().get(
                username__iexact=validated['username'])
        except Users.DoesNotExist:
            user_errors.append(INVALID_LOGIN)
            raise UserError(*user_errors)

        if not user_object.active:
            user_errors.append(_('user-inactive'))
            raise UserError(*user_errors)

        try:
            method_object = Methods.objects.get(user=user_object,
                method=METHOD_PASSWORD, step=1, status=METHOD_ACTIVE)
        except Methods.DoesNotExist:
            user_errors.append(INVALID_LOGIN)
            raise UserError(*user_errors)

        user_password = method_object.password.encode('utf-8')
        test_password = bcrypt.hashpw(validated['password'].encode('utf-8'),
                                      user_password)

        # Deletes original password to prevent later misuse
        del validated['password']

        if test_password != user_password:
            user_errors.append(INVALID_LOGIN)
            raise UserError(*user_errors)

        method_object.last_used = datetime.datetime.now(pytz.utc)
        method_object.save()

        if update_access:
            user_object.last_access = datetime.datetime.now(pytz.utc)
            user_object.save()

        return user_object

    def login_oath(self, token, **user_info):
        """Adds second step to password authentication by using OATH.

        Takes standard password credentials and passes them directly to
        login_password function. If successful, proceeds to test second
        authentication methods through use of a TOTP token.

        Args:
            token: token to authenticate against.
            user_info: passes to login_password.

        Returns:
            User object if successful.

        Raises:
            UserError: contains description.
        """

        user_errors = []

        # Attempts to login user through password
        user_object = self.login_password(update_access=False, **user_info)

        try:
            method_object = Methods.objects.get(user=user_object,
                method=METHOD_OATH_KEY, step=2, status=METHOD_ACTIVE)
        except Methods.DoesNotExist:
            user_errors.append(INVALID_LOGIN)
            raise UserError(*user_errors)

        user_current_token = otp.get_totp(method_object.token)

        if user_current_token != token:
            user_errors.append(INVALID_LOGIN)
            raise UserError(*user_errors)

        method_object.last_used = datetime.datetime.now(pytz.utc)
        method_object.save()

        user_object.last_access = datetime.datetime.now(pytz.utc)
        user_object.save()

        return user_object

    def login_recovery(self, **user_info):
        """Validates recovery email token against methods.

        Accepts username and token; both are required. Deactivates
        recovery token upon calling.

        Args:
            user_info: see above.

        Returns:
            User object if successful.

        Raises:
            UserError: contains description.
        """

        user_errors = []

        schema = Schema({
            Required('username', _('username-required')): All(str,
                msg=_('invalid-username')),
            Required('token', _('token-required')): All(str,
                Match(validators.VALID_TOKEN_REGEX), msg=_('invalid-token'))
        })

        try:
            validated = schema(user_info)

            del user_info
        except MultipleInvalid as error:
            user_errors = validators.list_errors(error)
            raise UserError(*user_errors)

        try:
            user_object = self.get_queryset().get(
                username__iexact=validated['username'], active=True)
        except Users.DoesNotExist:
            user_errors.append(INVALID_RECOVERY)
            raise UserError(*user_errors)

        try:
            method_object = Methods.objects.get(user=user_object,
                method=METHOD_RECOVERY_TOKEN,
                status=METHOD_ACTIVE,
                token=validated['token'])
        except Methods.DoesNotExist:
            user_errors.append(INVALID_RECOVERY)
            raise UserError(*user_errors)

        # Deactivates token regardless of expiration time
        method_object.status = METHOD_INACTIVE
        method_object.save()

        if method_object.expired():
            user_errors.append(INVALID_RECOVERY)
            raise UserError(*user_errors)

        method_object.last_used = datetime.datetime.now(pytz.utc)
        method_object.save()

        user_object.last_access = datetime.datetime.now(pytz.utc)
        user_object.save()

        return user_object


class AdminManager(UserManager):
    """Allows accessing admin accounts only and is admin-specific.

    Merely a mirror of the standard UserManager, but also allows custom
    admin related account functions.
    """

    # Limits this manager to admin users only.
    def get_queryset(self):
        """Overrides queryset to select only admins."""
        return super(UserManager, self).get_queryset().filter(
            user_type=USER_ADMIN)


class TokenManager(models.Manager):
    """Manager class for tokens. Provides token-related functions."""

    def generate(self, purpose, expiration_delta=TOKEN_TIME):
        """Generates token for given purpose and adds it to database.

        Args:
            purpose: use constant purpose flag.
            expiration_delta: datetime.timedelta

        Returns:
            Token object for token.
        """

        token = random_string(size=TOKEN_SIZE)
        token_data = dict()

        token_data['purpose'] = purpose
        token_data['token'] = token
        token_data['expiration'] = datetime.datetime.now(pytz.utc) + \
                                   expiration_delta

        token_object = self.get_queryset().create(**token_data)

        return token_object


class Users(models.Model):
    """Database model for user storage.

    Does not store user authentication methods. Fields requiring
    further explanation are as follows:
        user_type: integer describing the type of user. Includes the following:
            0: standard user
            1: admin user
    """

    users = UserManager()
    admins = AdminManager()

    username = models.CharField(max_length=50, unique=True)
    first_name = models.CharField(max_length=30, blank=True)
    last_name = models.CharField(max_length=30, blank=True)
    email = models.EmailField(max_length=75)
    user_type = models.IntegerField(default=0)
    active = models.BooleanField(default=True)
    last_access = models.DateTimeField(null=True)
    created = models.DateTimeField(auto_now_add=True)
    modified = models.DateTimeField(auto_now=True, auto_now_add=True)
    validated = models.BooleanField(default=False)

    def delete(self, *args, **kwargs):
        """Deletes users in user_obs.

        Use cautiously, and only for cron cleanups or admin removals.
        All related data must be archived before running this command,
        with an exception for methods, which will be cascaded.

        Args:
            All passed to superclass delete().

        Raises:
            UserError: if associated data present.
            RuntimeError: if user is not defined before deletion.
        """

        if not self.id:
            raise RuntimeError('User must be defined to delete.')

        user_errors = []

        try:
            super(Users, self).delete(*args, **kwargs)
        except Users.ProtectedError:
            user_errors.append(_('associated-data-present'))
            raise UserError(*user_errors)

    def deactivate(self):
        """Preferred method for "deletion." Sets user to deactivated.

        Also deletes all methods. Password must be modified later with
        check=False.

        This may be paired with a cron job to automatically delete
        users after being deactivated for a certain amount of time.
        Cron job may also remove user after archiving data. Locks user
        out of recreation for the time being.

        For now, all deactivated accounts remain deactivated.

        Raises:
            RuntimeError: if user is not defined before deactivation.
        """

        if not self.id:
            raise RuntimeError('User must be defined to set activation.')

        self.active = False
        self.save()

        method_list = Methods.objects.filter(user=self)
        method_list.delete()

    def activate(self):
        """Merely opposite of deactivate. Refer to deactivate."""

        if not self.id:
            raise RuntimeError('User must be defined to set activation.')

        self.active = True
        self.save()

    def modify_info(self, **user_info):
        """Validates and modifies user information for user.

        Note that this only allows modification of user details, such
        as username, names and email. It does not allow modification of
        settings such as user_type. For that, specific functions must
        be used.

        This function is provided to validate info and also disallow
        certain information to be directly modified.

        May set up a mechanism to change emails with re-validation.

        Args:
            user_info: passed to validator and then saved.

        Raises:
            RuntimeError: if user is not defined before modification.
        """

        if not self.id:
            raise RuntimeError('User must be defined to modify.')

        user_errors = []

        schema = Schema({
            'username': All(str, Match(validators.VALID_USERNAME_REGEX),
                msg=_('invalid-username')),
            'first_name': All(str, Match(validators.VALID_NAME_REGEX),
                msg=_('invalid-first-name')),
            'last_name': All(str, Match(validators.VALID_NAME_REGEX),
                msg=_('invalid-last-name')),
            'email': All(str, Match(validators.VALID_EMAIL_REGEX),
                msg=_('invalid-email')),
        })

        try:
            validated = schema(user_info)
        except MultipleInvalid as error:
            user_errors = validators.list_errors(error)
            raise UserError(*user_errors)

        # Sets instance variables to those of the validated schema and saves
        for key, value in list(validated.items()):
            setattr(self, key, value)
        self.save()

    def modify_password(self, new=None, check=True, old=None):
        """Optionally checks and sets password for user.

        Args:
            new: required; new password.
            check: checks if old password is correct.
            old: required if check is true; old password to test.

        Raises:
            RuntimeError: if user is not defined before modification.
        """

        if not self.id:
            raise RuntimeError('User must be defined to modify password.')

        user_errors = []

        if not new:
            user_errors.append(_('new-password-required'))

        schema = Schema({
            'password': All(str, validators.Password,
                msg=_('invalid-new-password')),
        })

        try:
            schema({'password': new})
        except MultipleInvalid as error:
            user_errors += list(validators.list_errors(error))

        password_method = Methods.objects.get(user=self,
            method=METHOD_PASSWORD)

        if check:
            if not old:
                user_errors.append(_('old-password-required'))
            else:
                user_password = password_method.password.encode('utf-8')
                test_password = bcrypt.hashpw(old.encode('utf-8').strip(),
                                            user_password)

                del old

                if test_password != user_password:
                    user_errors.append(_('invalid-old-password'))

        if user_errors:
            raise UserError(*user_errors)

        new_password = bcrypt.hashpw(new.encode('utf-8'),
            bcrypt.gensalt(SALT_ROUNDS))

        password_method.password = new_password
        password_method.save()

    def recover(self):
        """Creates recovery email token and sends it to user.

        Raises:
            RuntimeError: if user is not defined before recovery.
            UserError: if email fails.
        """

        if not self.id:
            raise RuntimeError('User must be defined to recover account.')

        user_errors = []

        # Deactivates all old recovery tokens
        Methods.objects.filter(user=self, method=METHOD_RECOVERY_TOKEN,
            status=METHOD_ACTIVE).update(status=METHOD_INACTIVE)

        email = self.email
        random_salt = random_string(size=TOKEN_SALT_SIZE)
        token = hashlib.sha1((email + random_salt).encode('utf-8')).hexdigest()

        method_object = Methods()
        method_object.user = self
        method_object.method = METHOD_RECOVERY_TOKEN
        method_object.token = token
        method_object.step = 0
        method_object.expiration = datetime.datetime.now() + VALIDATION_TIME
        method_object.save()

        subject = 'Recovery Token'
        text = 'Your account recovery token is: %s' % token

        try:
            send_mail(subject, text, settings.EMAIL_HOST_USER, [email])
        except:
            user_errors.append(_('recovery-email-failure'))
            raise UserError(*user_errors)

    def generate_oath(self):
        """Generates a TOTP oath key for 2nd step authentication.

        Encodes a ten-character random string and feeds it through a
        base32 encoding. Saves code to database and returns it for user
        user.

        Returns:
            key: oath key.

        Raises:
            RuntimeError: if user is not defined before generation.
        """

        if not self.id:
            raise RuntimeError('User must be defined for OATH generation.')

        Methods.objects.filter(user=self, method=METHOD_OATH_KEY).delete()

        random = random_string(size=OATH_STRING_SIZE)
        key = base64.b32encode(random.encode('utf-8'))

        method_object = Methods()
        method_object.user = self
        method_object.method = METHOD_OATH_KEY
        method_object.token = key
        method_object.step = 2
        method_object.save()

        return key


    def validate(self, token=None):
        """Validates account based on emailed token.

        Raises:
            RuntimeError: if user is not defined before validation.
            UserError: if token/username do not match.
        """

        if not self.id:
            raise RuntimeError('User must be defined to validate account.')

        user_errors = []

        if not token:
            user_errors.append(_('token-required'))
            raise UserError(*user_errors)

        schema = Schema({
            'token': All(str, Match(validators.VALID_TOKEN_REGEX),
                msg=_('invalid-token'))
        })

        try:
            validated = schema({'token': token})

            del token
        except MultipleInvalid as error:
            user_errors += list(validators.list_errors(error))
            raise UserError(*user_errors)

        try:
            method_object = Methods.objects.get(user=self,
                method=METHOD_VALIDATION_TOKEN,
                status=METHOD_ACTIVE,
                token=validated['token'])
        except Methods.DoesNotExist:
            user_errors.append(_('invalid-token'))
            raise UserError(*user_errors)

        method_object.status = METHOD_INACTIVE
        method_object.last_used = datetime.datetime.now(pytz.utc)
        method_object.save()

        if method_object.expired():
            user_errors.append(_('token-expired'))
            raise UserError(*user_errors)

        self.validated = True
        self.save()

    def set_admin(self):
        """Sets user_type to that of an admin.

        Raises:
            RuntimeError: if user is not defined before setting.
        """

        if not self.id:
            raise RuntimeError('User must be defined to update status.')

        self.user_type = USER_ADMIN
        self.save()

    def set_standard(self):
        """Sets user_type to that of a standard user.

        Raises:
            RuntimeError: if user is not defined before setting.
        """

        if not self.id:
            raise RuntimeError('User must be defined to update status.')

        self.user_type = USER_STANDARD
        self.save()

    def __str__(self):
        return self.username


class Methods(models.Model):
    """Database model for authentication methods.

    Fields requiring further explanation are as follows:
        method: integer representing method type:
            0: password
            1: validation token
        password/token:
            only one may be defined; password hashes are often
                shorter and quicker and are not arbitrary.
        step:
            for multi-step authentication, important to be defined.
                Numbered by number of step, i.e. 1 for first step.
        status: current availability status of the method for the user.
            1: active
            0: inactive
    """

    user = models.ForeignKey(Users, on_delete=models.CASCADE)
    method = models.IntegerField()
    password = models.CharField(max_length=60, blank=True)
    token = models.TextField(blank=True)
    step = models.IntegerField()
    status = models.IntegerField(default=1)
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True, auto_now_add=True)
    last_used = models.DateTimeField(null=True)
    expiration = models.DateTimeField(null=True)

    def expired(self):
        """Checks to see if method has been expired yet.

        Returns:
            True if expired, false if not.
        """

        # Returns true if no expiration date
        if not self.expiration:
            return False

        return datetime.datetime.now(pytz.utc) > self.expiration

    def __str__(self):
        return str(self.method)


class Tokens(models.Model):
    """Model for tokens used throughout project.

    This model is not intended for individual tokens, i.e., email
    validation tokens. It is meant for system-wide tokens.

    Tokens must be either letters or numbers, that's it.
    """

    objects = TokenManager()

    purpose = models.CharField(max_length=30)
    token = models.CharField(max_length=50, unique=True)
    exhausted = models.BooleanField(default=False)
    expiration = models.DateTimeField(null=True)
    created = models.DateTimeField(auto_now_add=True)

    # Defines helper functions

    def expired(self):
        """Checks to see if token has been expired or not.

        Returns:
            True if expired, false if not.
        """

        # Returns not expired if no expiration date
        if not self.expiration:
            return False

        return datetime.datetime.now(pytz.utc) > self.expiration

    def __str__(self):
        return self.purpose