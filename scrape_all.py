import requests
import json
import re
import time

# --- CONFIGURATION ---
PROPERTY_ID = 51

# 1. Provide your active Expedia cookies here
COOKIE_STRING = "HMS=d5a59812-3f50-398a-ac76-fbfc24fff83e; MC1=GUID=537a40d7b6624469aae6ebc920023ad4; DUAID=537a40d7-b662-4469-aae6-ebc920023ad4; linfo=v.4,|0|0|255|1|0||||||||1033|0|0||0|0|0|-1|-1; cookie_prompt=enabled; VERIFY_URL_BANNER=enabled; ak_bmsc=427FC73FF0457167CA2B49F61742BDB1~000000000000000000000000000000~YAAQi3AsMTqAPQ+gAQAAvxu6PACQnstNcTwTNCx0GVkL/clh+SL9vO/49tGkcK0EkwskpsgXOln3D1ACkbMbIplRfleyZ502TaKvkPzvyI0y6BwFrOtXZaYxhrs4trLtHZO7zyExjiok092FIuXPOj2sP6uoOnYmwmGA3YZM3Lw2f9qc2+M1+HGsX8jdMDtuE63nEmDYlxQHrxyFkJACdbhSDSeQ4bt6A4lB5sNHXldyr5Ak0f/hRXq6VGRk78gXZHW+lN4SBHJeOh+q73JwfegfxLwHsuhpkiR21m8yr7Rkbd33hvcvKXLl6aJPiwiA5dNQMqrbao+5wBVwDh0+tlXZ9ANWzHVmjL/L5awjl6CE1Me4kjHzygme4FP1MOZ/aSwEWby/FynG9JmXKCQ3rBXdnsrUJAi/n4v52w==; EPCSession=af1e2451d262772a216baf99d78e6a3a; ssoidp=oidc:eg; mdid=ZSwpIecWlJR_mmQjF8x-ZJWua_Od3ab7QM72baipCm2fTJ8LY_IN_-XXNLTantMqQvTlgsdRNfVcIj6-ls3zpA; EG_SESSIONTOKEN=bHmGI2eRQKKZx4860Iw10bLZ713nIAZ7-zxpQTN12u4:N8ESJkDNjdPLbU7VyVCoqPJdpah8lREMvehSXwkijdi2Db5iOYXB7Vw9b9TTydXaYTuZfTSuFPjtQtw6VDczaw; HMS=d5a59812-3f50-398a-ac76-fbfc24fff83e; DUAID=537a40d7-b662-4469-aae6-ebc920023ad4; MC1=GUID=537a40d7b6624469aae6ebc920023ad4; CRQS=t|101`s|2056`l|en_US`c|USD; CRQSS=e|1; currency=USD; iEAPID=1; tpid=v.1,101; JSESSIONID=0FB97E86218C8FF4A3423C4075455B7B; PIM-SESSION-ID=zbZ5609qBPo3S0CV; _abck=7694ED2C746BFF7656ABF6043FFB085E~0~YAAQi3AsMWJjPw+gAQAAVgzOPBC+D+HWlDcsiIlrGPvSi6X3fQl9ALjwCM7sQPk4dlfrixGv+hf1DnaviGRCH258lH9RWmiR5gCQOZPKmqzJTiffA1kNj+AM9uc+1HedGx8EFCht38BE9hu3hkSfweSaWRXwOmtt7Q3+PtnJiKP0ZwANvmOX1meKGIusqJric6ni2ubM+HkTQtHkG6pfQVWnzHJhktmqURXxgnYjK2D8UdI2mC5bOSsH4DJpbjVhWiSNc4R6vxFIeOMwzj6yfQ+wRR9m/3Cix2MukGY80WwTwA3xq/SxS263uMXrJeHpkWVIO1KDFad/k5wn9o4Pc/hEDOt9bycXW5TY2QFTEhPfhGCvEEBi/udRJHHvZExas5HsIASmY6oZesNAGLT0u5kZPalCZ1aOjYVVzIFn+5vVGi5CUSQkxov5TtKfbn7DtPRV3y0n1TMLNck+RNamkQx9z4hEkj7emLcJ3vEgy7OwS5JfPH+QsMvp/FaJRkLbDGhdMMEb8blA7wmjGebj3naXFoGeDxGq377SNc38muVmcM3taFr8DiTAWcKKD9CQfZualbIKYQeyvfjnlc623lhrM4yJgZeugI9lsd29kQp766HnwPKHOWJlMJ/q5+ROuDGIjkLJOJtyGOeG2kBQ7moeob4HGurhSymsZG7CGiC1esknPvb69k+fhPbs7I1aaEfYmc1CvyVkXbmyG06mrltnPjyv0A2FzC1fNeVHXRTlLhx73ub81zG3/Lxqd/7aANx7oryxbKhDRdpJHpLt0KXyIHjp51SVdoedzM9cgZ7LXDI3DvVjDPb/rtAGKM/NaJdRae31anFfkIzFNmNmufxYbjbomOQ5T++6c8e72A6zoKAlAZbJtdI9Z8ggy29wxNzdq4RgiKQuBtUxA2mxDtDQo6QoQ9OEFWBArKz6KXYGdb92uAr7r4RDRYoUufrlkBWYzMGjDr7xNmcs+wL5zSE66+mgnLECrML/tn+XZDyV96xXGHnXV846tcH4ES0lXh8=~-1~-1~-1~AAQAAAAG%2f%2f%2f%2f%2f0qK%2fR7oLicDC68pcPiqtvKCwBgM2x5fPdgvvjmiyQmUbY2GLsdv%2f%2fW+TCbzE7OfA0hXS7OeRNOLRwKpnq6HjXU0fc+GRa2sEicThCN9owaB1YQUnc7PZdy71FpyS7ioSzOi3nc%3d~-1; bm_s=YAAQi3AsMZN+Qg+gAQAANgHiPAVwFVPah8+IqDeguInQmVBjT0q2ed7aQdXB6V2JMZ3L946JbFXm+7WHUW88df8Gqa87+F6hHthMP8XiCBYYBS9ywulJxR7iFduDUQAahyByCMaAnWoYkD0MMrcRNsukhZMTTt327CxeEaoW6cjNH8Gt+olBnaNtLAggZfeqkjDlVcTTM0UbK1xs9At3dFVONWZ2Egmb1yUEc23XxfgrFegfd1D4rbaU0U8DaMYR2dmn5peifRaI7lM8s3urn8DK7AIGKVGvJ49Wp7JoCAAkSdhUX/O+sSYNOQ7slxUTrpR6hntZFXGKH1pQCR18JSK7Eex7t5Oa47rW2ASVYvGkiWN1atJt2GLHkfixgTz77p76pH+fx9WFHPyVaP/4pu6g9cLJioKxYvCE7B5/4Owp0pEXfsYX+pVlMbSWbiE+riOsRxsJoXFWTFP2npA9g59UEk8vBXRRQsZ1kOuq2Z3vTXic8JKgkfuGtxPLBpZnzeM4iAYcGrYhufCAryc4IluRpesAv8qe+nELTm/jbx1mtEpwkoDDCst8fLkEJEbdLFJPbb4u/3LZXIp/WPIZbmUiIjk8F1ffX3+zT+JLB+2WjqJwkBFTo5PXtJndFxKZkfDjev8bVOyfFjxM7wEDRL8LvPheG5QzDOmBqq13NhejmmCGzis/yB1R4/+kS6Tc3dbZCdmnNKRVV4+xp3/tiZAta0B9Yn8cppuq1NmhnXi9WSjFkwc67HDGRV/wBmk9zg2zwa6T6MqJLpsPoHzF6u2kJcTHtET7WOMNiRa3RexsjFaeWLXA1UJKNMg6OxzwyhOZ8ykuvXI1gOXFm8Nqj4dUMrwj0ybzTnBbQ/lpDYEuZCeSxrFQiKhMIqc1nPt6+Dv9D3cjXSwo1k9ujjwSponi+ZmJupiYkUAS51VDok6yOc4ui7zlzrUTrJYmYWUc7Nf36SPZT9rlfDmJaDqWiPFnk4wdCwiIcU0dCV6ov5d3zN4XPzYmnbCubTuPL8iLgHAIJ5i42blyFl4TUCTPKp32iFe7u0whtiFh5ZUi0+bEKRZj0Pt4KCfVZ2Zu2GSUSW3pBgOtkTzJ8/w=; bm_so=4CB98A43F29C6C54843EB13B6D4DE2FBBC5523A07CA6BA5C076209D9D620F760~YAAQi3AsMZR+Qg+gAQAANgHiPAhThES0cauNCjZ/N0INfLatmHDKWqEnvqQsfbZ920rzjzG2AQ6EQJ9seGee3PNHY90CW3RoUt+r+LuV7fWZ4mdYCXGprUAkb7rrhyztz+i3QQDy5qCQXCl/Ex5lEARfuQ0l7FBjmGZIOeCHh8FGJVCo0mAScc2As0zZyClqOuDfW4wzzGa5EzhraDiqn4sGBl6jg7GFfTDJVZBlwE+OQxSeD3e1uuZt3U59Ge36UOW/lJPbAHN33whYFHx8x3/K0uAETw4uO6ZJ1m9mrtSkTGfSuN4wjhu3ICFc267SnzO0HU4rwRY1EQh9euAUkTcBU3JY2sHGoCtzGLm/LL+De8dCVrMXP0SNGpn8uU9/moAW+saWUWorh9K0hqN8r5WzSi3EpV3ZbsTLP+HaG3pBmKSV67XohLvvGjVi99EpPUwBCiM8/MDizzwnZ56yi85CMqhwtSwS3P408WwaSfOLfg==; bm_sz=342D4566D00E1639F2503FF029FE22AB~YAAQi3AsMZZ+Qg+gAQAANgHiPABESyb7BFvJkSIjNoQJTaFFrxGLXW80swnJ+v6nRNHdpOiQEqSPCXhh6InZTedaAooGv0iwDXGoR7DZ7Xyasbg9NBdYnAmbZ9dDrHIzoEckBeFE2WyJYatOAfrzMaKTcIIdMUmsVntZHLRL3jT76kG/02NmduHhn82tKSQ0V9i4SYuLftLntUznyV5a7n+fzkUqpe7AvPbsok9h1vfPcIoPOZSxoXnqW7Ud5dWgc3VO6hZD9Ose9/QUD254CSdAHDXTX0JNnH7cOB5rJk5gL4MTG08ociAaQETrrT+eKAF2P0JlNyeeRICPK+L8FoWcZQNaBNhDuvkg5YWrAVEEV8Ire1JLKk8xMDlTeuS/SsnqcI+lxUJRuoJTui4n6wPuNN1SMiz5sWzaCH/2UMR7DufjP/OMB5KGNxerGf0aA1DluKrtgV5qqmajK2farUpEl04/U9nT64SdNnM/GlmuD0+BsyuTzay7DurBIiKZlU2XovB4ePIn210k9GqYjXhI4l1213D7/rFj9yBJDsO3BvTjg4qqyvJjHECrDlpShk6bQejUHFxsA==~4601922~3225414; epcsid=eyJraWQiOiJlc3MtZGlyMSIsImFsZyI6ImRpciIsImVuYyI6IkExMjhDQkMtSFMyNTYifQ..dTaejI-xM7XtB3DCYPSKxQ.7xZ_bnfXAhbVA1knliovJK-Ir0POhexcU2aml8c6rUTLv4C9UxH6unImCXcuqdCj2SYCa_EkL0pqracXoLwIWNI-KF3P1pGtxyo-WjOJOuOcnprnAmnpRe40r57jd4tk7aYCAOjAL0GIjCR3io8Xi71ChrqeLDzK9-ywYkVf091EPoMdx0MuJV18xcthVHw7gK4OAr1ItGL6lqo9H1XKz1HD4TRYYEwlZVXcrzi5lWx0jYmX6z3SWobJdAdIJKDQevMxB88tJ4wxNRiceKEtp_zAIEwDRnBmNYOi6rgyHVvRJRwzcyapzriX5Y0b4_jwkb1YFcrPU38iOImLm1N3WP_2gZmw6G9_p636T7ehX3d_i3TxiLsWIvrzOc0578RPu-NUYSLyHDEQ0fbNQVBWZkaY-ZaR2B0NMacvgrlF-d4.WxXaYnPy4s4StWCIyKxtAQ; bm_sv=052CA28220BD628E6F23B4AF93992C97~YAAQi3AsMcl/Qg+gAQAAzQ/iPAAXjx1Z5pOTx2y6Pe3ezvY+hX/B4eAyh6sqbxot6fLeAsDLkpyWyPztkSYce02Ak+Kzrkbsr56s/tDCSRu1SBaSCc9P0F0Kq05de1a9rDo+7iAOsAW+JrPOP5lcgcSgQTMmKDt811V+xKfDsAUEGogOrE8lPOSCOLRnF8BRtz3hltAQqcED/y819VsXtX0ZC+aDeiqdXa+dbwVEsCU28b0NEaZtRbUBX0FS5wN47TKUckwRRLLCtZFMOsoDEac=~1"

def get_reservations_list(session):
    url = "https://api.expediapartnercentral.com/supply/experience/gateway/graphql"
    headers = {
        "accept": "*/*",
        "client-name": "pc-reservations-web",
        "content-type": "application/json",
        "cookie": COOKIE_STRING,
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
    }
    
    payload = {
        "query": """query getReservationsBySearchCriteria {
            reservationSearchV2(input: {
                propertyId: %s, 
                booked: true, 
                canceled: true, 
                confirmed: true, 
                startDate: "2026-08-25", 
                endDate: "2026-08-26", 
                dateType: "checkIn", 
                evc: false, 
                expediaCollect: true, 
                timezoneOffset: "-04:00", 
                hotelCollect: true, 
                isSpecialRequest: false, 
                isVIPBooking: false, 
                reconciled: false, 
                readyToReconcile: false, 
                returnBookingItemIDsOnly: false, 
                unconfirmed: true, 
                searchForCancelWaiversOnly: false 
            }) { 
                reservationItems { 
                    reservationItemId 
                    reservationInfo {
                        startDate
                        endDate
                        createDateTime
                        product { unitName }
                    }
                    customer { id guestName }
                    confirmationInfo { productConfirmationCode }
                    totalAmounts { propertyBookingTotal { value currencyCode } }
                } 
            }
        }""" % PROPERTY_ID,
        "variables": {}
    }

    print("Fetching reservations list from GraphQL...")
    response = session.post(url, headers=headers, json=payload)
    if response.status_code == 200:
        data = response.json()
        items = data.get("data", {}).get("reservationSearchV2", {}).get("reservationItems", [])
        
        parsed_reservations = []
        for item in items:
            res_info = item.get("reservationInfo", {})
            total_amounts = item.get("totalAmounts", {}).get("propertyBookingTotal", {})
            
            res_data = {
                "Guest": item.get("customer", {}).get("guestName"),
                "Reservation": item.get("reservationItemId"),
                "Confirmation": item.get("confirmationInfo", {}).get("productConfirmationCode"),
                "Check-in": res_info.get("startDate"),
                "Check-out": res_info.get("endDate"),
                "Room": res_info.get("product", {}).get("unitName"),
                "Booked on": res_info.get("createDateTime"),
                "Booking amount": f"{total_amounts.get('value', 'N/A')} {total_amounts.get('currencyCode', '')}".strip()
            }
            parsed_reservations.append(res_data)
        
        print(f"Successfully fetched {len(parsed_reservations)} reservations.")
        return parsed_reservations
    else:
        print("Failed to fetch reservations list:", response.text)
        return []

def get_reservation_details(session, reservation_id):
    url = "https://apps.expediapartnercentral.com/lodging/reservations/legacyReservationDetails.html"
    headers = {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "cookie": COOKIE_STRING,
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
    }
    
    params = {
        "htid": str(PROPERTY_ID),
        "reservationIds": reservation_id
    }
    
    response = session.get(url, headers=headers, params=params)
    if response.status_code == 200:
        html = response.text
        match = re.search(r'var\s+jsonPayload\s*=\s*(\{.*?\});', html, re.DOTALL)
        if match:
            json_str = match.group(1)
            data = json.loads(json_str)
            res_val = data.get("value", [{}])[0]
            
            # Map detailed fields
            payment = res_val.get("payment", {})
            payment_info = res_val.get("paymentInfo", {})
            evc = payment_info.get("evcInfo", {})
            customer_card = payment_info.get("customerCardInfo", {})
            booking_info = res_val.get("bookingInfo", {})
            amounts = res_val.get("bookingAmounts", {}).get("lineItems", [])
            total_amounts = res_val.get("totalAmounts", {})
            
            mapped_data = {
                "Payment Information": payment.get("supplier_payment_instruction"),
                "EVC Card Number": evc.get("cardNumber") if evc else None,
                "EVC Expires": f"{evc.get('expirationDate', {}).get('month')}/{evc.get('expirationDate', {}).get('year')}" if evc and evc.get("expirationDate") else None,
                "EVC CVV": evc.get("cvv") if evc else None,
                "EVC Billing Address": evc.get("billingAddress") if evc else None,
                "Real Card Number": customer_card.get("cardNumber") if customer_card else None,
                "Real Expires": customer_card.get("expirationDate") if customer_card else None,
                "Real CVV": customer_card.get("cvv") if customer_card else None,
                "Real Billing Address": customer_card.get("billingAddress") if customer_card else None,
                "Room Type Details": booking_info.get("roomTypeName"),
                "Nightly rates": [item for item in amounts if item.get("type") == "DAILY_RATE"],
                "Subtotal": next((item.get("costAmount") for item in amounts if item.get("type") == "SUB_TOTAL"), None),
                "Total payout": total_amounts.get("totalBookingAmount", {}).get("amount"),
                "Hotel confirmation code": booking_info.get("hotelConfirmationCode"),
                "Status": booking_info.get("status"),
                "Itinerary number": booking_info.get("itineraryNumber"),
                "Reservation made": booking_info.get("bookingDate"),
                "Pricing model": booking_info.get("pricingModel"),
                "IATA/TIDS #": booking_info.get("IATANumber"),
                "Bedding request": booking_info.get("bedTypeName"),
                "Rate plan code": booking_info.get("ratePlanCode"),
                "Rate plan name": booking_info.get("ratePlanName"),
                "Guest count": booking_info.get("adultCount", 0) + (booking_info.get("childCount") or 0),
                "Cancellation Policy": res_val.get("cancelPolicy", {}),
                "Reservation History": res_val.get("history", {})
            }
            return mapped_data
    return {}

def main():
    with requests.Session() as session:
        # Step 1: Grab list of all reservations
        reservations = get_reservations_list(session)
        
        if not reservations:
            print("No reservations to process.")
            return

        final_data = {}
        
        # Step 2: Loop through each one to get detailed info
        for idx, res in enumerate(reservations):
            res_id = res.get("Reservation")
            conf_code = res.get("Confirmation")
            
            if not res_id or not conf_code:
                continue
                
            print(f"Fetching details for ID: {res_id} ({idx + 1}/{len(reservations)})...")
            details = get_reservation_details(session, res_id)
            
            # Merge list data + detail data
            merged_res = res.copy()
            merged_res.update(details)
            
            final_data[conf_code] = merged_res
            time.sleep(0.5) # Sleep to avoid rate limiting
            
        # Step 3: Save final dictionary to reservations_output.json
        with open("reservations_output.json", "w") as f:
            json.dump(final_data, f, indent=4)
            
        print(f"\nAll done! Successfully scraped and merged {len(final_data)} reservations into reservations_output.json.")

if __name__ == "__main__":
    main()
