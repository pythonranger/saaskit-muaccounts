import re, socket, csv
from random import random

from django import forms
from django.conf import settings
from django.contrib.auth.models import User
from django.utils.safestring import SafeUnicode
from django.utils.translation import ugettext_lazy as _
from django.utils.hashcompat import sha_constructor
from django.template.loader import render_to_string

from gdata.contacts.service import ContactsService
import gdata
from friends.importer import import_vcards
from friends.models import Contact, JoinInvitation, send_mail
from friends.forms import JoinRequestForm
from registration.forms import RegistrationFormUniqueEmail
from registration.signals import user_registered, user_activated
from emailconfirmation.models import EmailAddress
from uni_form.helpers import FormHelper, Submit, Reset, Layout, Fieldset

from muaccounts.models import MUAccount, InvitationRequest
from themes import ThemeField

class SubdomainInput(forms.TextInput):

    def render(self, *args, **kwargs):
        return SafeUnicode(
            super(SubdomainInput,self).render(*args,**kwargs)
            + MUAccount.subdomain_root )

class MUAccountBaseForm(forms.ModelForm):
    
    class Meta:
        model=MUAccount
    
    def __init__(self, *args, **kwargs):
        super(MUAccountBaseForm, self).__init__(*args, **kwargs)
        if 'subdomain' in self.fields:
            self.fields['subdomain'].widget = SubdomainInput()
        if 'theme' in self.fields:
            self.fields['theme'] = ThemeField(required=True)
                
    _subdomain_re = re.compile('^[a-z0-9][a-z0-9-]+[a-z0-9]$')
    def clean_subdomain(self):
        subdomain = self.cleaned_data['subdomain'].lower().strip()

        if not self._subdomain_re.match(subdomain):
            raise forms.ValidationError(
                _('Invalid subdomain name.  You may only use a-z, 0-9, and "-".'))

        for pattern in getattr(settings, 'MUACCOUNTS_SUBDOMAIN_STOPWORDS', (r"^www$",)):
            if re.search(pattern, subdomain, re.I):
                raise forms.ValidationError(
                    _('It is not allowed to use this domain name.'))

        return subdomain
    
    _domain_re = re.compile(r'^[a-z0-9][a-z0-9-]*\.[a-z0-9-.]+[a-z0-9]$')
    def clean_domain(self):
        if not self.cleaned_data.get('domain'):
            return
        
        d = self.cleaned_data['domain'].strip().lower()
        
        if not self._domain_re.match(d):
            raise forms.ValidationError('Invalid domain name.')

        if d.endswith(MUAccount.subdomain_root):
            raise forms.ValidationError(
                _('You cannot set subdomain of %s.') % MUAccount.subdomain_root)

        try:
            ip = socket.gethostbyname(d)
            if hasattr(settings, 'MUACCOUNTS_IP'):
                if callable(settings.MUACCOUNTS_IP):
                    if not settings.MUACCOUNTS_IP(ip):
                        self._errors['domain'] = forms.util.ErrorList([
                            _('Domain %s does not resolve to a correct IP number.') % d ])
                else:
                    if ip != settings.MUACCOUNTS_IP:
                        self._errors['domain'] = forms.util.ErrorList([
                            _('Domain %(domain)s does not resolve to %(ip)s.') % {'domain':d, 'ip':settings.MUACCOUNTS_IP} ])
        except socket.error, msg:
            self._errors['domain'] = forms.util.ErrorList([
                _('Cannot resolve domain %(domain)s: %(error_string)s')%{'domain':d,'error_string':msg} ])

        return d

    
class MUAccountForm(MUAccountBaseForm):
    
    # this displays how to attach a formHelper to your forms class.
    helper = FormHelper()
    helper.add_input(Submit('submit',_('Save')))
    is_multipart = True
    
    def __init__(self, *args, **kwargs):
        super(MUAccountForm, self).__init__(*args, **kwargs)
        if 'owner' in self.fields:
            self.fields['owner'].widget = forms.HiddenInput()
    
class InvitationRequestForm(forms.ModelForm):
    # this displays how to attach a formHelper to your forms class.
    helper = FormHelper()
    helper.add_input(Submit('submit',_('Send request')))
    
    def __init__(self, *args, **kwargs):
        super(InvitationRequestForm, self).__init__(*args, **kwargs)
        if 'muaccount' in self.fields:
            self.fields['muaccount'].widget = forms.HiddenInput()
    
    class Meta:
        model = InvitationRequest
        fields = ('email', 'notes', 'muaccount')
    
    
class ImportVCardForm(forms.Form):

    vcard_file = forms.FileField(label="vCard File")

    def save(self, user):
        imported, total = import_vcards(self.cleaned_data["vcard_file"].content, user)
        return imported, total


class ImportCSVContactsForm(forms.Form):
    
    csv_file = forms.FileField(label=_("CSV file"),
        help_text = _("Format of each row: \"contact name\",\"e-mail address\". Rows with wrong format will be skiped."))
    
    def clean_csv_file(self):
        """just iterate over file"""
        try:
            for row in csv.reader(self.cleaned_data['csv_file']):
                pass
        except csv.Error, msg:
            raise forms.ValidationError(_("Error while reading. Check your file."))
                
        return self.cleaned_data['csv_file']
    
    def save(self, user):
        total, imported = 0, 0
        for row in csv.reader(self.cleaned_data['csv_file']):
            if row:
                try:
                    name, email = row
                except ValueError:
                    #default behaviour
                    continue
                
                total +=1
                try:
                    Contact.objects.get(user=user, email=email)
                except Contact.DoesNotExist:
                    Contact(user=user, name=name, email=email).save()
                    imported += 1
        return imported, total

class ImportGoogleContactsForm(forms.Form):
    
    email = forms.EmailField()
    password = forms.CharField(widget=forms.PasswordInput)
    
    def clean(self):
        if 'email' in self.cleaned_data and 'password' in self.cleaned_data:
            contacts_service = ContactsService(self.cleaned_data['email'], self.cleaned_data['password'])
            try:
                contacts_service.ProgrammaticLogin()
            except gdata.service.BadAuthentication, msg:
                raise forms.ValidationError(_(u'Incorrect Google account credentials'))
        return self.cleaned_data
    
    def save(self, user):
        contacts_service = ContactsService(self.cleaned_data['email'], self.cleaned_data['password'])
        contacts_service.ProgrammaticLogin()
        #based on django-friends importer module
        entries = []
        feed = contacts_service.GetContactsFeed()
        entries.extend(feed.entry)
        next_link = feed.GetNextLink()
        while next_link:
            feed = contacts_service.GetContactsFeed(uri=next_link.href)
            entries.extend(feed.entry)
            next_link = feed.GetNextLink()
        total = 0
        imported = 0
        for entry in entries:
            name = entry.title.text
            for e in entry.email:
                email = e.address
                total += 1
                try:
                    Contact.objects.get(user=user, email=email)
                except Contact.DoesNotExist:
                    Contact(user=user, name=name, email=email).save()
                    imported += 1
        return imported, total

_existing_emails = lambda muaccount: EmailAddress.objects.filter(
                                                    user__muaccount_member = muaccount, 
                                                    verified=True)

class MuJoinRequestForm(forms.Form):
    
    email = forms.EmailField(label=_("Email"), required=False, widget=forms.TextInput(attrs={'size':'30'}), help_text=_('user email'))
    contacts = forms.models.ModelMultipleChoiceField(queryset=Contact.objects.all(),
                                                     required=False, label=_('Contacts'), help_text=_('contact list'))
    message = forms.CharField(label="Message", required=False, 
                              widget=forms.Textarea(attrs = {'cols': '30', 'rows': '5'}), help_text=_('invitation message'))
    muaccount = forms.IntegerField(widget=forms.HiddenInput())
    
    def __init__(self, data=None, files=None, initial=None, *args, **kwargs):
        super(MuJoinRequestForm, self).__init__(data=data, files=files, initial=initial, *args, **kwargs)
        muaccount = self.data.get('muaccount') or self.initial.get('muaccount')
        self.fields['contacts'].queryset = self.fields['contacts'].queryset\
                .filter(user__owned_sites=muaccount)\
                .exclude(email__in=_existing_emails(muaccount).values_list('email', flat=True))
    
    def save(self, user):
        contacts = list(self.cleaned_data.get('contacts', []))
        if self.cleaned_data.get('email'):
            contact, created = Contact.objects.get_or_create(email=self.cleaned_data['email'], user=user)
            if contact not in contacts:
                contacts.append(contact)

        muaccount = MUAccount.objects.get(id=self.cleaned_data['muaccount'])
        message = self.cleaned_data['message']
        context = {
            "SITE_NAME": muaccount.name,
            "CONTACT_EMAIL": user.email or settings.CONTACT_EMAIL,
            "user": user,
            "message": message,
        }
        
        for contact in contacts:
            #BASED ON django-friends JoinInvitationManager's method 'send_invitation' 
            contact, created = Contact.objects.get_or_create(email=contact.email, user=user)
            salt = sha_constructor(str(random())).hexdigest()[:5]
            confirmation_key = sha_constructor(salt + contact.email).hexdigest()
            context['accept_url'] = muaccount.get_absolute_url('friends_accept_join', 
                                                               args=(confirmation_key,))
            
            subject = render_to_string("friends/join_invite_subject.txt", context)
            email_message = render_to_string("friends/join_invite_message.txt", context)
            
            send_mail(subject, email_message, settings.DEFAULT_FROM_EMAIL, [contact.email])        
            join_request = JoinInvitation.objects.get_or_create(from_user=user, contact=contact, 
                                                 message=message, status="2", 
                                                 confirmation_key=confirmation_key)
            user.message_set.create(message=_("Invitation to join sent to %(email)s") 
                                                % {'email':contact.email})

#Muaccount specific content

class AddFormMixin(object):
    
    def __init__(self, *args, **kwargs):
        super(AddFormMixin, self).__init__(*args, **kwargs)
        self.fields['muaccount'].widget = forms.HiddenInput()


class ChangeFormMixin(object):
    
    def save(self, commit=True):
        if self.instance.pk is not None and self.instance.muaccount is None:
            #flush instance
            self.instance = self.instance.__class__()
        
        return super(ChangeFormMixin, self).save(commit)
    save.alters_data = True
