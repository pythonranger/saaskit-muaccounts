#based on pinax local application bbauth
import ybrowserauth

from django.conf import settings
from django.http import HttpResponseRedirect
from django.core.urlresolvers import reverse

def login(request, redirect_to="invitations_contacts"): # @@@ redirect_to should not be hard-coded here
    ybbauth = ybrowserauth.YBrowserAuth(request.muaccount.yahoo_app_id, 
                                        request.muaccount.yahoo_secret)
    yahoo_login = ybbauth.getAuthURL(appd=reverse(redirect_to))
    return HttpResponseRedirect(yahoo_login)

def success(request):
    if "appid" in request.GET:
        ybbauth = ybrowserauth.YBrowserAuth(request.muaccount.yahoo_app_id, 
                                            request.muaccount.yahoo_secret)
        ts = request.GET["ts"]
        sig = request.GET["sig"]
        appdata = request.GET["appdata"]
        REQUEST_URI = request.path + "?" + request.META['QUERY_STRING']
        #ybbauth.validate_sig(ts, sig, REQUEST_URI)
        # add token to session for now
        request.session['bbauth_token'] = request.GET["token"]
        return HttpResponseRedirect(appdata)
    else:
        pass # @@@

def logout(request, redirect_to="/"):
    del request.session['bbauth_token']
    return HttpResponseRedirect(redirect_to)
