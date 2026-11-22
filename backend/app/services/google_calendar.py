from datetime import datetime, timezone
import os
import pickle
from google_auth_oauthlib.flow import Flow
from google.auth.transport.requests import Request as GoogleAuthRequest
from googleapiclient.discovery import build
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse
from app.config import (
    GOOGLE_CLIENT_SECRET_FILE, 
    GOOGLE_API_SCOPES, 
    API_HOST, 
    API_PORT,
    OAUTH_REDIRECT_PATH
)
import logging

logger = logging.getLogger(__name__)

class GoogleCalendarService:
    def __init__(self):
        self.credentials = None
        self.token_path = 'token.pickle'
        self.service = None
        # בשרת, החזרה מגוגל חייבת להגיע לכתובת הציבורית — localhost
        # של השרת אינו נגיש מהדפדפן של רון. מקומית נשאר localhost.
        public = os.getenv("PUBLIC_URL", "").rstrip("/")
        self.redirect_uri = (
            f"{public}{OAUTH_REDIRECT_PATH}" if public
            else f"http://{API_HOST}:{API_PORT}{OAUTH_REDIRECT_PATH}"
        )
    
    def get_auth_url(self):
        """Generate and return the Google OAuth authorization URL."""
        flow = Flow.from_client_secrets_file(
            GOOGLE_CLIENT_SECRET_FILE,
            scopes=GOOGLE_API_SCOPES,
            redirect_uri=self.redirect_uri
        )

        auth_url, state = flow.authorization_url(
            access_type='offline',
            include_granted_scopes='true',
            prompt='consent'
        )

        # Store only the state
        with open("oauth_state.txt", "w") as f:
            f.write(state)

        # store client_config and redirect_uri to use in the callback function.
        with open("client_config.pickle", "wb") as f:
            pickle.dump({
                "client_secrets_file": GOOGLE_CLIENT_SECRET_FILE,
                "scopes": GOOGLE_API_SCOPES,
                "redirect_uri": self.redirect_uri,
                # PKCE: הספרייה מייצרת code_verifier בזמן authorization_url.
                # ה-callback בונה אובייקט Flow חדש, ובלי לשמור אותו כאן
                # החלפת ה-code נכשלת ב-"Missing code verifier".
                "code_verifier": getattr(flow, "code_verifier", None),
            }, f)

        return auth_url

    async def handle_oauth_callback(self, request: Request):
        """Handle the OAuth callback and exchange code for token"""
        code = request.query_params.get('code')
        state = request.query_params.get('state')

        if not code or not state:
            raise HTTPException(status_code=400, detail="Missing code or state")

        try:
            with open('oauth_state.txt', 'r') as f:
                saved_state = f.read()
            with open('client_config.pickle', 'rb') as f:
                client_config = pickle.load(f)

        except FileNotFoundError:
            raise HTTPException(status_code=500, detail="Authentication flow expired")

        if state != saved_state:
            raise HTTPException(status_code=400, detail="Invalid state parameter")

        try:
            flow = Flow.from_client_secrets_file(
                client_config["client_secrets_file"],
                scopes=client_config["scopes"],
                redirect_uri=client_config["redirect_uri"]
            )
            if client_config.get("code_verifier"):
                flow.code_verifier = client_config["code_verifier"]
            flow.fetch_token(code=code)
            self.credentials = flow.credentials

            with open(self.token_path, 'wb') as token:
                pickle.dump(self.credentials, token)

            self.service = build('calendar', 'v3', credentials=self.credentials)
            html_content = """
                <!DOCTYPE html>
                <html>
                <head>
                    <title>Authentication Successful</title>
                    <style>
                        body {
                            font-family: Arial, sans-serif;
                            text-align: center;
                            margin-top: 50px;
                        }
                        .container {
                            max-width: 500px;
                            margin: auto;
                            padding: 20px;
                            border: 1px solid #ddd;
                            border-radius: 10px;
                            box-shadow: 2px 2px 12px rgba(0, 0, 0, 0.1);
                        }
                        h2 {
                            color: #4CAF50;
                        }
                        p {
                            font-size: 16px;
                        }
                    </style>
                </head>
                <body>
                    <div class="container">
                        <h2>Authentication Successful</h2>
                        <p>You can now close this tab.</p>
                    </div>
                </body>
                </html>
                """

            return HTMLResponse(content=html_content, status_code=200)

            # return {"message": "Authentication successful! Pllease close this tab."}
        except Exception as e:
            logger.error(f"Error during token exchange: {e}")
            raise HTTPException(status_code=500, detail="Failed to authenticate")

    def get_calendar_service(self):
        """Get an authenticated Google Calendar service."""
        if self.service:
            logger.info("✅ Using existing service instance.")
            return self.service

        # Load existing credentials if available
        if os.path.exists(self.token_path):
            try:
                with open(self.token_path, 'rb') as token:
                    self.credentials = pickle.load(token)
            except Exception as e:
                logger.info(f"⚠️ Error loading credentials: {e}")
                if os.path.exists(self.token_path): 
                    os.remove(self.token_path)
                return None  # Force re-authentication

        # If credentials are valid, use them
        if self.credentials and self.credentials.valid:
            logger.info("✅ Loaded valid credentials.")
            self.service = build('calendar', 'v3', credentials=self.credentials)
            return self.service

        # If credentials are expired but refreshable, refresh them
        if self.credentials and self.credentials.expired and self.credentials.refresh_token:
            try:
                logger.info("🔄 Refreshing expired credentials...")
                self.credentials.refresh(GoogleAuthRequest())
                with open(self.token_path, 'wb') as token:
                    pickle.dump(self.credentials, token)
                logger.info("✅ Credentials refreshed successfully.")
                self.service = build('calendar', 'v3', credentials=self.credentials)
                return self.service
            except Exception as e:
                logger.info(f"❌ Failed to refresh credentials: {e}")
                if os.path.exists(self.token_path):
                    os.remove(self.token_path)  # Remove invalid credentials
                return None

        # If no valid credentials are found, require authentication
        logger.info("⚠️ No valid credentials found. User must reauthenticate.")
        return None
    
    def is_authenticated(self):
        """Check if the user is authenticated"""
        return self.get_calendar_service() is not None
    
    def get_user_timezone(self):
        """Fetch the user's time zone from Google Calendar settings."""
        service = self.get_calendar_service()
        if not service:
            return 'UTC'  # Default to UTC if authentication fails

        try:
            settings = service.settings().get(setting='timezone').execute()
            return settings.get('value', 'UTC')
        except Exception as e:
            logger.info(f"⚠️ Failed to retrieve user time zone: {e}")
            return 'UTC'
    
    def create_event(self, event_data):
        """Create a new event in Google Calendar"""
        service = self.get_calendar_service()
        if not service:
            return {
                'success': False,
                'message': 'Authentication required',
                'auth_required': True
            }
        
        # Get user's time zone
        user_timezone = self.get_user_timezone()
            
        event = {
            'summary': event_data.get('event_name', 'Untitled Event'),
            'description': event_data.get('description', ''),
            'start': {
                'dateTime': f"{event_data.get('date')}T{event_data.get('start_time')}:00",
                'timeZone': user_timezone,
            },
            'end': {
                'dateTime': f"{event_data.get('date')}T{event_data.get('end_time')}:00",
                'timeZone': user_timezone,
            },
        }
        
        # Add attendees if specified
        if event_data.get('participants'):
            event['attendees'] = [
                {'email': participant} for participant in event_data.get('participants')
                if '@' in participant  # Simple email validation
            ]
        
        created_event = service.events().insert(calendarId='primary', body=event).execute()
        return {
            'success': True,
            'event_id': created_event['id'],
            'event_link': created_event['htmlLink']
        }
    
    def update_event(self, event_id, event_data):
        """Update an existing event in Google Calendar"""
        service = self.get_calendar_service()
        if not service:
            return {
                'success': False,
                'message': 'Authentication required',
                'auth_required': True
            }
            
        # First retrieve the event
        event = service.events().get(calendarId='primary', eventId=event_id).execute()
        
        # Update fields
        if 'event_name' in event_data:
            event['summary'] = event_data['event_name']
        if 'description' in event_data:
            event['description'] = event_data['description']
        if 'date' in event_data and 'start_time' in event_data:
            event['start']['dateTime'] = f"{event_data['date']}T{event_data['start_time']}:00"
        if 'date' in event_data and 'end_time' in event_data:
            event['end']['dateTime'] = f"{event_data['date']}T{event_data['end_time']}:00"
        
        updated_event = service.events().update(
            calendarId='primary', eventId=event_id, body=event).execute()
            
        return {
            'success': True,
            'event_id': updated_event['id'],
            'event_link': updated_event['htmlLink']
        }
    
    def delete_event(self, event_id):
        """Delete an event from Google Calendar"""
        service = self.get_calendar_service()
        if not service:
            return {
                'success': False,
                'message': 'Authentication required',
                'auth_required': True
            }
            
        service.events().delete(calendarId='primary', eventId=event_id).execute()
        return {'success': True, 'message': 'Event deleted successfully'}
    
    def query_events(self, query_params):
        """Query events based on parameters"""
        service = self.get_calendar_service()
        if not service:
            return {
                'success': False,
                'message': 'Authentication required',
                'auth_required': True
            }
        
        date_str = query_params.get('date')  
        if date_str:  
            try:
                date_obj = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                time_min = date_obj.isoformat()  # Start of day
                time_max = date_obj.replace(hour=23, minute=59, second=59).isoformat()  # End of day
            except ValueError:
                return {'success': False, 'message': 'Invalid date format. Use YYYY-MM-DD'}


        query_text = query_params.get('event_name')
        # logger.info(f"Querying events with time: {time_min} to {time_max}")

        request = service.events().list(
            calendarId='primary',
            timeMin=time_min if time_min else None,
            timeMax=time_max if time_max else None,
            # q=query_text if query_text else None,
            singleEvents=True,
            orderBy='startTime'
        )

        events_result = request.execute()
        events = events_result.get('items', [])
        
        if not events:
            return {'success': False, 'message': 'No matching events found'}

        return {
            'success': True,
            'events': [
                {
                    'id': event['id'],
                    'summary': event.get('summary', 'No Title'),
                    'start': event['start'].get('dateTime', event['start'].get('date')),
                    'end': event['end'].get('dateTime', event['end'].get('date')),
                    'participants': event.get('attendees', []),
                    'description': event.get('description', ''),
                    'link': event.get('htmlLink', ''),
                }
                for event in events
            ]
        }

    def list_calendars(self):
        """Check if authentication works by listing calendars"""
        if not self.is_authenticated():
            return "You are not authenticated. Please log in."

        calendars = self.service.calendarList().list().execute()
        return calendars.get("items", [])
